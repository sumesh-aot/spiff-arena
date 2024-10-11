import json
from collections import OrderedDict
from collections.abc import Generator
from typing import Any
from flask import jsonify, make_response
from datetime import datetime

import flask.wrappers
import sentry_sdk
from flask import current_app
from flask import g
from flask import jsonify
from flask import make_response
from flask import stream_with_context
from flask.wrappers import Response
from SpiffWorkflow.bpmn.exceptions import WorkflowTaskException  # type: ignore
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow  # type: ignore
from SpiffWorkflow.task import Task as SpiffTask  # type: ignore
from SpiffWorkflow.util.task import TaskState  # type: ignore
from sqlalchemy import and_
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import AliasedClass

from spiffworkflow_backend.constants import SPIFFWORKFLOW_BACKEND_SERIALIZER_VERSION
from spiffworkflow_backend.data_migrations.process_instance_migrator import ProcessInstanceMigrator
from spiffworkflow_backend.exceptions.api_error import ApiError
from spiffworkflow_backend.exceptions.error import HumanTaskAlreadyCompletedError
from spiffworkflow_backend.models.db import SpiffworkflowBaseDBModel
from spiffworkflow_backend.models.db import db
from spiffworkflow_backend.models.group import GroupModel
from spiffworkflow_backend.models.human_task import HumanTaskModel
from spiffworkflow_backend.models.human_task_user import HumanTaskUserModel
from spiffworkflow_backend.models.process_model import ProcessModelInfo
from spiffworkflow_backend.models.json_data import JsonDataModel
from spiffworkflow_backend.models.process_instance import ProcessInstanceModel
from spiffworkflow_backend.models.process_instance import ProcessInstanceModelSchema
from spiffworkflow_backend.models.process_instance import ProcessInstanceStatus
from spiffworkflow_backend.models.process_instance import ProcessInstanceTaskDataCannotBeUpdatedError
from spiffworkflow_backend.models.process_instance_event import ProcessInstanceEventType
from spiffworkflow_backend.models.task import Task
from spiffworkflow_backend.models.task import TaskModel
from spiffworkflow_backend.models.task_definition import TaskDefinitionModel
from spiffworkflow_backend.models.task_draft_data import TaskDraftDataDict
from spiffworkflow_backend.models.task_draft_data import TaskDraftDataModel
from spiffworkflow_backend.models.task_instructions_for_end_user import TaskInstructionsForEndUserModel
from spiffworkflow_backend.models.user import UserModel
from spiffworkflow_backend.routes.process_api_blueprint import _find_principal_or_raise
from spiffworkflow_backend.routes.process_api_blueprint import _find_process_instance_by_id_or_raise
from spiffworkflow_backend.routes.process_api_blueprint import _find_process_instance_for_me_or_raise
from spiffworkflow_backend.routes.process_api_blueprint import _get_process_model
from spiffworkflow_backend.routes.process_api_blueprint import _get_task_model_for_request, _get_task_model_by_guid
from spiffworkflow_backend.routes.process_api_blueprint import _get_task_model_from_guid_or_raise
from spiffworkflow_backend.routes.process_api_blueprint import _munge_form_ui_schema_based_on_hidden_fields_in_task_data
from spiffworkflow_backend.routes.process_api_blueprint import _task_submit_shared
from spiffworkflow_backend.routes.process_api_blueprint import _update_form_schema_with_task_data_as_needed
from spiffworkflow_backend.services.authorization_service import AuthorizationService
from spiffworkflow_backend.services.error_handling_service import ErrorHandlingService
from spiffworkflow_backend.services.jinja_service import JinjaService
from spiffworkflow_backend.services.process_instance_processor import ProcessInstanceProcessor
from spiffworkflow_backend.services.process_instance_queue_service import ProcessInstanceIsAlreadyLockedError
from spiffworkflow_backend.services.process_instance_queue_service import ProcessInstanceQueueService
from spiffworkflow_backend.services.process_instance_service import ProcessInstanceService
from spiffworkflow_backend.services.process_instance_tmp_service import ProcessInstanceTmpService
from spiffworkflow_backend.services.task_service import TaskService
from .tasks_controller import _get_tasks, task_assign
import time


def filter_tasks(body: dict, firstResult: int = 1, maxResults: int = 100)  -> flask.wrappers.Response:
    """Filter tasks and return the list."""
    if not body or body.get('criteria') is None:
        return None
    user_model: UserModel = g.user

    human_tasks_query = (
        db.session.query(HumanTaskModel, ProcessInstanceModel.id, ProcessModelInfo)
        .group_by(
            HumanTaskModel.id,  # Group by the ID of the human task
            ProcessInstanceModel.id,  # Add the process instance ID to the GROUP BY clause
            ProcessModelInfo.process_id
        )  # type: ignore
        .outerjoin(GroupModel, GroupModel.id == HumanTaskModel.lane_assignment_id)
        .join(ProcessInstanceModel)
        .join(ProcessModelInfo, ProcessModelInfo.id == ProcessInstanceModel.process_model_identifier)
        .filter(
            HumanTaskModel.completed == False,  # noqa: E712
            ProcessInstanceModel.status != ProcessInstanceStatus.error.value,
        )
    )

    # Join through HumanTaskUserModel to associate users to tasks
    human_tasks_query = human_tasks_query.outerjoin(
        HumanTaskUserModel,
        and_(HumanTaskModel.id == HumanTaskUserModel.human_task_id, HumanTaskUserModel.ended_at_in_seconds == None)
    ).outerjoin(UserModel, UserModel.id == HumanTaskUserModel.user_id)  # Join UserModel using HumanTaskUserModel

    # Check candidateGroupsExpression with value ${currentUserGroups()}
    if body.get('criteria').get('candidateGroupsExpression') == '${currentUserGroups()}':
        human_tasks_query = human_tasks_query.filter(
            GroupModel.identifier.in_([group.identifier for group in user_model.groups]))
    if candidate_group := body.get('criteria').get('candidateGroup'):
        human_tasks_query = human_tasks_query.filter(GroupModel.identifier == candidate_group)
    if body.get('criteria').get('includeAssignedTasks', False):
        human_tasks_query = human_tasks_query
    else:
        human_tasks_query = human_tasks_query.filter(~HumanTaskModel.human_task_users.any())

    if process_def_key := body.get('criteria').get('processDefinitionKey'):
        human_tasks_query = human_tasks_query.filter(ProcessInstanceModel.process_model_identifier == process_def_key)
    if ''.join(body.get('criteria').get('assigneeExpression', '').split()) == '${currentUser()}':
        human_tasks_query = human_tasks_query.filter(UserModel.username == user_model.username)

    # TODO body.get('criteria').get('assignee', '')
    # TODO body.get('criteria').get('processVariables', '')
    # TODO body.get('criteria').get('sorting', '')



    user_username_column = func.max(UserModel.username).label("process_initiator_username")
    user_displayname_column = func.max(UserModel.display_name).label("process_initiator_firstname")
    user_email_column = func.max(UserModel.email).label("process_initiator_email")
    group_identifier_column = func.max(GroupModel.identifier).label("assigned_user_group_identifier")

    human_tasks = (
        human_tasks_query.add_columns(
            user_username_column,
            user_displayname_column,
            user_email_column,
            group_identifier_column,
            HumanTaskModel.task_name,
            HumanTaskModel.task_title,
            HumanTaskModel.process_model_display_name,
            HumanTaskModel.process_instance_id,
            HumanTaskModel.updated_at_in_seconds,
            HumanTaskModel.created_at_in_seconds
        )
        .order_by(desc(HumanTaskModel.id))  # type: ignore
        .paginate(page=firstResult, per_page=maxResults, error_out=False)
    )

    return _format_response(human_tasks)


def get_task_by_id(
    task_id: str
) -> flask.wrappers.Response:
    # Query to join HumanTaskModel with HumanTaskUserModel
    task_query = (
        db.session.query(HumanTaskModel, HumanTaskUserModel, UserModel)
        .join(HumanTaskUserModel, and_(HumanTaskModel.id == HumanTaskUserModel.human_task_id, HumanTaskUserModel.ended_at_in_seconds == None))
        .join(UserModel, HumanTaskUserModel.user_id == UserModel.id)  # Join with UserModel to get user details
        .filter(HumanTaskModel.task_guid == task_id)
    )

    tasks = task_query.all()

    # If no tasks are found, return an empty list
    if not tasks:
        raise ApiError(
            error_code="task_not_found",
            message=f"Cannot find a task with id '{task_id}'",
            status_code=400,
        )
    if not len(tasks) > 1:
        raise ApiError(
            error_code="more_than_one_task_found",
            message=f"More tasks found for '{task_id}'",
            status_code=400,
        )
    human_task, human_task_user, user_model = tasks[0]
    return make_response(jsonify(format_human_task_response(human_task, user_model)), 200)


def claim_task(
    task_id: str,
body: dict[str, Any],
) -> flask.wrappers.Response:
    task_model: HumanTaskModel | None = HumanTaskModel.query.filter_by(id=task_id).one_or_none()
    if task_model is None:
        raise ApiError(
            error_code="task_not_found",
            message=f"Cannot find a task with id '{task_id}'",
            status_code=400,
        )

    task_assign(modified_process_model_identifier=None, process_instance_id=task_model.process_instance_id, task_guid= task_model.task_guid,body={'user_ids': [body.get("userId")]})

    return make_response(jsonify(format_human_task_response(task_model)), 200)

def unclaim_task(
    task_id: str,
body: dict[str, Any],
) -> flask.wrappers.Response:
    task_model: HumanTaskModel | None = HumanTaskModel.query.filter_by(id=task_id).one_or_none()
    if task_model is None:
        raise ApiError(
            error_code="task_not_found",
            message=f"Cannot find a task with id '{task_id}'",
            status_code=400,
        )

    # formsflow.ai allows only one user per task.
    human_task_users = HumanTaskUserModel.query.filter_by(ended_at_in_seconds=None, human_task=task_model).all()
    for human_task_user in human_task_users:
        human_task_user.ended_at_in_seconds = round(time.time())

    SpiffworkflowBaseDBModel.commit_with_rollback_on_exception()

    return make_response(jsonify({"ok": True}), 200)


def get_task_variables( #TODO
    task_id: int
) -> flask.wrappers.Response:
    pass

def get_task_identity_links( #TODO
    task_id: int
) -> flask.wrappers.Response:
    pass

def submit_task(
    task_id: str,
body: dict[str, Any],
) -> flask.wrappers.Response:
    task_model: HumanTaskModel | None = HumanTaskModel.query.filter_by(id=task_id).one_or_none()
    if task_model is None:
        raise ApiError(
            error_code="task_not_found",
            message=f"Cannot find a task with id '{task_id}'",
            status_code=400,
        )
    # TODO Manage task variables submitted.
    with sentry_sdk.start_span(op="controller_action", description="tasks_controller.task_submit"):
        response_item = _task_submit_shared(task_model.process_instance_id, task_model.task_guid, body)
        return make_response(jsonify(response_item), 200)





def _format_response(human_tasks):
    response = []

    tasks = []
    for task in human_tasks.items:
        task_data = {
            "_links": {
                # pass empty _links as spiff doesn't support HATEOAS
            },
            "_embedded": {
                "candidateGroups": [
                    {
                        "_links": {
                            "group": {
                                "href": f"/group/{task.group_identifier_column}"
                            },
                            "task": {
                                "href": f"/task/{task.HumanTaskModel.id}"
                            }
                        },
                        "_embedded": None,
                        "type": "candidate",
                        "userId": None,  # TODO Find User ID
                        "groupId": task.group_identifier_column,
                        "taskId": task.HumanTaskModel.id
                    }
                ],
                "variable": []  # TODO Retrieve from the task data
            },
            "id": task.HumanTaskModel.task_guid,
            "name": task.HumanTaskModel.task_name,
            "assignee": task.user_username_column,
            "created": datetime.utcfromtimestamp(task.HumanTaskModel.created_at_in_seconds).isoformat() + 'Z',
            "due": None,  # TODO
            "followUp": None,  # TODO
            "delegationState": None,
            "description": None,
            "executionId": task.HumanTaskModel.process_instance_id,
            "owner": None,
            "parentTaskId": None,
            "priority": 50, #TODO
            "processDefinitionId": task.ProcessModelInfo.process_id,
            "processInstanceId": task.HumanTaskModel.process_instance_id,
            "taskDefinitionKey": task.HumanTaskModel.task_id,
            "caseExecutionId": None,
            "caseInstanceId": None,
            "caseDefinitionId": None,
            "suspended": False,
            "formKey": None,
            "camundaFormRef": None,
            "tenantId": None  # TODO
        }

        tasks.append(task_data)

    assignees = [
        {
            "_links": {
                "self": {
                    "href": f"/user/{task.user_username_column}"
                }
            },
            "_embedded": None,
            "id": task.user_username_column,
            "firstName": task.user_displayname_column,  # Replace with actual data
            "lastName": "",  # Replace with actual data
            "email": task.user_email_column  # Replace with actual data
        }
        for task in human_tasks.items
    ]

    process_definitions = [
        {
            "_links": {},
            "_embedded": None,
            "id": task.ProcessModelInfo.id,
            "key": task.ProcessModelInfo.process_id,  # Replace with actual data
            "category": "http://bpmn.io/schema/bpmn",
            "description": task.ProcessModelInfo.description,
            "name": task.ProcessModelInfo.display_name,
            "versionTag": "1",  # TODO Replace with actual version if available
            "version": 1,  # TODO Replace with actual version if available
            "resource": f"{task.ProcessModelInfo.display_name}.bpmn",
            "deploymentId": task.ProcessModelInfo.id,
            "diagram": None,
            "suspended": False,
            "contextPath": None
        }
        for task in human_tasks.items
    ]

    response.append({
        "_links": {},
        "_embedded": {
            "assignee": assignees,
            "processDefinition": process_definitions,
            "task": tasks
        },
        "count": human_tasks.total
    })

    response.append({  # TODO Add additional information
        "variables": [
            {
                "name": "formName",
                "label": "Form Name"
            },
            {
                "name": "applicationId",
                "label": "Submission Id"
            }
        ],
        "taskVisibleAttributes": {
            "applicationId": True,
            "assignee": True,
            "taskTitle": True,
            "createdDate": True,
            "dueDate": True,
            "followUp": True,
            "priority": True,
            "groups": True
        }
    })

    return make_response(jsonify(response), 200)


def format_human_task_response(human_task: HumanTaskModel, user_model: UserModel) -> dict:
    """
    Format the human_task into the required response structure.
    """
    return {
        "id": human_task.task_guid,
        "name": human_task.task_title or human_task.task_name,
        "assignee": user_model.username,
        "created": datetime.utcfromtimestamp(human_task.created_at_in_seconds).isoformat() + "Z" if human_task.created_at_in_seconds else None,
        "due": None,  #TODO
        "followUp": None,  #TODO
        "description": human_task.task_name,  # Assuming task_name serves as the description
        "parentTaskId": None,  # No clear parent task id field in the model
        "priority": 50,  # Default to 50 since there's no priority field in the model
        "processDefinitionId": human_task.bpmn_process_identifier,  # Mapping to bpmn_process_identifier
        "processInstanceId": human_task.process_instance_id,
        "taskDefinitionKey": human_task.task_id,  # Mapping taskDefinitionKey to task_id
        "tenantId": None  # TODO
    }

