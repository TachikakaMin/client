import logging
import sys

import wandb
from wandb.apis.internal import Api
from wandb.errors import ExecutionException, LaunchException

from ._project_spec import create_project_from_spec, fetch_and_validate_project
from .agent import LaunchAgent
from .runner import loader
from .utils import (
    _is_wandb_local_uri,
    construct_run_spec,
    PROJECT_DOCKER_ARGS,
    PROJECT_SYNCHRONOUS,
)

if wandb.TYPE_CHECKING:
    from .runner.abstract import AbstractRun
    from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


def push_to_queue(api: Api, queue: str, run_spec: Dict[str, Any]) -> Any:
    try:
        res = api.push_to_run_queue(queue, run_spec)
    except Exception as e:
        print("Exception:", e)
        return None
    return res


def run_agent(entity: str, project: str, queues: Optional[List[str]] = None) -> None:
    agent = LaunchAgent(entity, project, queues)
    agent.loop()


def _run(
    uri: str,
    experiment_name: Optional[str],
    wandb_project: Optional[str],
    wandb_entity: Optional[str],
    docker_image: Optional[str],
    entry_point: Optional[str],
    version: Optional[str],
    parameters: Optional[Dict[str, Any]],
    docker_args: Optional[Dict[str, Any]],
    resource: str,
    launch_config: Optional[Dict[str, Any]],
    synchronous: Optional[bool],
    api: Api,
) -> AbstractRun:
    """
    Helper that delegates to the project-running method corresponding to the passed-in backend.
    Returns a ``SubmittedRun`` corresponding to the project run.
    """

    run_spec = construct_run_spec(
        uri,
        experiment_name,
        wandb_project,
        wandb_entity,
        docker_image,
        entry_point,
        version,
        parameters,
        launch_config,
    )
    project = create_project_from_spec(run_spec, api)
    project = fetch_and_validate_project(project, api)

    # construct runner config.
    runner_config: Dict[str, Any] = {}
    runner_config[PROJECT_SYNCHRONOUS] = synchronous
    runner_config[PROJECT_DOCKER_ARGS] = docker_args

    backend = loader.load_backend(resource, api, runner_config)
    if backend:
        submitted_run = backend.run(project)
        return submitted_run
    else:
        raise ExecutionException(
            "Unavailable backend {}, available backends: {}".format(
                resource, ", ".join(loader.WANDB_RUNNERS.keys())
            )
        )


def run(
    uri: str,
    api: Api,
    entry_point: Optional[str] = None,
    version: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
    docker_args: Optional[Dict[str, Any]] = None,
    experiment_name: Optional[str] = None,
    resource: str = "local",
    wandb_project: Optional[str] = None,
    wandb_entity: Optional[str] = None,
    docker_image: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    synchronous: Optional[bool] = True,
) -> AbstractRun:
    """
    Run a W&B project. The project can be local or stored at a Git URI.
    W&B provides built-in support for running projects locally or remotely on a Databricks or
    Kubernetes cluster. You can also run projects against other targets by installing an appropriate
    third-party plugin. See `Community Plugins <../plugins.html#community-plugins>`_ for more
    information.
    For information on using this method in chained workflows, see `Building Multistep Workflows
    <../projects.html#building-multistep-workflows>`_.
    :raises: :py:class:`wandb.exceptions.ExecutionException` If a run launched in blocking mode
             is unsuccessful.
    :param uri: URI of project to run. A local filesystem path
                or a Git repository URI pointing to a project directory containing an MLproject file.
    :param entry_point: Entry point to run within the project. If no entry point with the specified
                        name is found, runs the project file ``entry_point`` as a script,
                        using "python" to run ``.py`` files and the default shell (specified by
                        environment variable ``$SHELL``) to run ``.sh`` files.
    :param version: For Git-based projects, either a commit hash or a branch name.
    :param parameters: Parameters (dictionary) for the entry point command.
    :param docker_args: Arguments (dictionary) for the docker command.
    :param experiment_name: Name of experiment under which to launch the run.
    :param resource: Execution backend for the run: W&B provides built-in support for "local" backend
    :param wandb_project: Target project to send launched run to
    :param wandb_entity: Target entity to send launched run to
    :param config: A dictionary which will be passed as config to the backend. The exact content
                           which should be provided is different for each execution backend
    :param synchronous: Whether to block while waiting for a run to complete. Defaults to True.
                        Note that if ``synchronous`` is False and ``backend`` is "local", this
                        method will return, but the current process will block when exiting until
                        the local run completes. If the current process is interrupted, any
                        asynchronous runs launched via this method will be terminated. If
                        ``synchronous`` is True and the run fails, the current process will
                        error out as well.
    :return: :py:class:`wandb.launch.SubmittedRun` exposing information (e.g. run ID)
             about the launched run.
    .. code-block:: python
        :caption: Example
        import wandb
        project_uri = "https://github.com/wandb/examples"
        params = {"alpha": 0.5, "l1_ratio": 0.01}
        # Run W&B project and create a reproducible docker environment
        # on a local host
        api = wandb.apis.internal.Api()
        wandb.launch(project_uri, api, parameters=params)
    .. code-block:: text
        :caption: Output
        ...
        ...
        Elasticnet model (alpha=0.500000, l1_ratio=0.010000):
        RMSE: 0.788347345611717
        MAE: 0.6155576449938276
        R2: 0.19729662005412607
        ... wandb.launch: === Run (ID '6a5109febe5e4a549461e149590d0a7c') succeeded ===
    """
    if docker_args is None:
        docker_args = {}
    if _is_wandb_local_uri(api.settings("base_url")):
        if sys.platform == "win32":
            docker_args["net"] = "host"
        else:
            docker_args["network"] = "host"
        if sys.platform == "linux" or sys.platform == "linux2":
            docker_args["add-host"] = "host.docker.internal:host-gateway"

    if config is None:
        config = {}

    submitted_run_obj = _run(
        uri=uri,
        experiment_name=experiment_name,
        wandb_project=wandb_project,
        wandb_entity=wandb_entity,
        docker_image=docker_image,
        entry_point=entry_point,
        version=version,
        parameters=parameters,
        docker_args=docker_args,
        resource=resource,
        launch_config=config,
        synchronous=synchronous,
        api=api,
    )

    if synchronous:
        _wait_for(submitted_run_obj)
    else:
        raise LaunchException("Non synchronous mode not supported")
    return submitted_run_obj


def _wait_for(submitted_run_obj: AbstractRun) -> None:
    """Wait on the passed-in submitted run, reporting its status to the tracking server."""
    # Note: there's a small chance we fail to report the run's status to the tracking server if
    # we're interrupted before we reach the try block below
    try:
        if submitted_run_obj.wait():
            _logger.info("=== Submitted run succeeded ===")
        else:
            raise ExecutionException("Submitted run failed")
    except KeyboardInterrupt:
        _logger.error("=== Submitted run interrupted, cancelling run ===")
        submitted_run_obj.cancel()
        raise
