import asyncio
from collections import defaultdict
from enum import Enum
import time
from typing import Dict, List, Optional, Tuple

import ray
import ray.cloudpickle as pickle
from ray.actor import ActorHandle
from ray.serve.async_goal_manager import AsyncGoalManager
from ray.serve.backend_worker import create_backend_replica
from ray.serve.common import (
    BackendInfo,
    BackendTag,
    Duration,
    GoalId,
    ReplicaTag,
)
from ray.serve.config import BackendConfig, ReplicaConfig
from ray.serve.constants import LongPollKey
from ray.serve.kv_store import RayInternalKVStore
from ray.serve.long_poll import LongPollHost
from ray.serve.utils import format_actor_name, get_random_letters, logger

CHECKPOINT_KEY = "serve-backend-state-checkpoint"
SLOW_STARTUP_WARNING_S = 30
SLOW_STARTUP_WARNING_PERIOD_S = 30


class ReplicaState(Enum):
    SHOULD_START = 1
    STARTING = 2
    RUNNING = 3
    SHOULD_STOP = 4
    STOPPING = 5
    STOPPED = 6


class BackendReplica:
    def __init__(self, controller_name: str, detached: bool,
                 replica_tag: ReplicaTag, backend_tag: BackendTag,
                 version: str):
        self._actor_name = format_actor_name(replica_tag, controller_name)
        self._placement_group_name = self._actor_name + "_placement_group"
        self._controller_name = controller_name
        self._detached = detached
        self._replica_tag = replica_tag
        self._backend_tag = backend_tag
        self._actor_handle = None
        self._placement_group = None
        self._start_time = None
        self._prev_slow_startup_warning_time = None
        self._startup_obj_ref = None
        self._drain_obj_ref = None
        self._state = ReplicaState.SHOULD_START
        self._actor_resources = None
        self._version = version

    def __get_state__(self):
        clean_dict = self.__dict__.copy()
        del clean_dict["_placement_group"]
        del clean_dict["_actor_handle"]
        del clean_dict["_startup_obj_ref"]
        del clean_dict["_drain_obj_ref"]
        return clean_dict

    def __set_state__(self, d):
        self.__dict__ = d
        self._actor_handle = None
        self._startup_obj_ref = None
        self._drain_obj_ref = None
        self._recover_from_checkpoint()

    @property
    def version(self):
        return self._version

    def _recover_from_checkpoint(self):
        if self._state == ReplicaState.STARTING:
            # We do not need to pass in the class here because the actor
            # creation has already been started if this class was checkpointed
            # in the STARTING state.
            self.start()
        elif self._state == ReplicaState.RUNNING:
            # Fetch actor handles for all backend replicas in the system.
            # The actors must exist if this class was checkpointed in the
            # RUNNING state.
            self._placement_group = ray.util.get_placement_group(
                self._placement_group_name)
            self._actor_handle = ray.get_actor(self._actor_name)
        elif self._state == ReplicaState.STOPPING:
            self.stop()

    def start(self, backend_info: Optional[BackendInfo]):
        assert self._state in {
            ReplicaState.SHOULD_START, ReplicaState.STARTING
        }, (f"State must be {ReplicaState.SHOULD_START} or "
            f"{ReplicaState.STARTING}, *not* {self._state}")

        if self._actor_resources is None:
            self._actor_resources = backend_info.replica_config.resource_dict

        try:
            self._placement_group = ray.util.get_placement_group(
                self._placement_group_name)
        except ValueError:
            logger.debug(
                "Creating placement group '{}' for backend '{}'".format(
                    self._placement_group_name, self._backend_tag))
            self._placement_group = ray.util.placement_group(
                [self._actor_resources],
                lifetime="detached",
                name=self._placement_group_name)

        try:
            self._actor_handle = ray.get_actor(self._actor_name)
        except ValueError:
            logger.debug("Starting replica '{}' for backend '{}'.".format(
                self._replica_tag, self._backend_tag))
            self._actor_handle = ray.remote(backend_info.worker_class).options(
                name=self._actor_name,
                lifetime="detached" if self._detached else None,
                max_restarts=-1,
                max_task_retries=-1,
                placement_group=self._placement_group,
                **backend_info.replica_config.ray_actor_options).remote(
                    self._backend_tag, self._replica_tag,
                    backend_info.replica_config.init_args,
                    backend_info.backend_config, self._controller_name)
        self._start_time = time.time()
        self._prev_slow_startup_warning_time = time.time()
        self._startup_obj_ref = self._actor_handle.ready.remote()
        self._state = ReplicaState.STARTING

    def check_started(self):
        if self._state == ReplicaState.RUNNING:
            return True
        assert self._state == ReplicaState.STARTING, (
            f"State must be {ReplicaState.STARTING}, *not* {self._state}")
        ready, _ = ray.wait([self._startup_obj_ref], timeout=0)
        if len(ready) == 1:
            self._state = ReplicaState.RUNNING
            return True

        time_since_start = time.time() - self._start_time
        if (time_since_start > SLOW_STARTUP_WARNING_S
                and time.time() - self._prev_slow_startup_warning_time >
                SLOW_STARTUP_WARNING_PERIOD_S):
            # Filter to relevant resources.
            required = {
                k: v
                for k, v in self._actor_resources.items() if v > 0
            }
            available = {
                k: v
                for k, v in ray.available_resources().items() if k in required
            }
            logger.warning(
                f"Replica '{self._replica_tag}' for backend "
                f"'{self._backend_tag}' has taken more than "
                f"{time_since_start:.0f}s to start up. This may be "
                "caused by waiting for the cluster to auto-scale or "
                "because the backend constructor is slow. Resources required: "
                f"{required}, resources available: {available}.")
            self._prev_slow_startup_warning_time = time.time()

        return False

    def set_should_stop(self, graceful_shutdown_timeout_s: Duration):
        self._state = ReplicaState.SHOULD_STOP
        self._graceful_shutdown_timeout_s = graceful_shutdown_timeout_s

    def stop(self):
        # We need to handle transitions from:
        #  SHOULD_START -> SHOULD_STOP -> STOPPING
        # This means that the replica_handle may not have been created.

        assert self._state in {
            ReplicaState.SHOULD_STOP, ReplicaState.STOPPING
        }, (f"State must be {ReplicaState.SHOULD_STOP} or "
            f"{ReplicaState.STOPPING}, *not* {self._state}")

        def drain_actor(actor_name):
            # NOTE: the replicas may already be stopped if we failed
            # after stopping them but before writing a checkpoint.
            try:
                replica = ray.get_actor(actor_name)
            except ValueError:
                return None
            return replica.drain_pending_queries.remote()

        self._state = ReplicaState.STOPPING
        self._drain_obj_ref = drain_actor(self._actor_name)
        self._shutdown_deadline = time.time(
        ) + self._graceful_shutdown_timeout_s

    def check_stopped(self):
        if self._state == ReplicaState.STOPPED:
            return True
        assert self._state == ReplicaState.STOPPING, (
            f"State must be {ReplicaState.STOPPING}, *not* {self._state}")

        try:
            replica = ray.get_actor(self._actor_name)
        except ValueError:
            self._state = ReplicaState.STOPPED
            return True

        ready, _ = ray.wait([self._drain_obj_ref], timeout=0)
        timeout_passed = time.time() > self._shutdown_deadline

        if len(ready) == 1 or timeout_passed:
            if timeout_passed:
                # Graceful period passed, kill it forcefully.
                logger.debug(
                    f"{self._actor_name} did not shutdown after "
                    f"{self._graceful_shutdown_timeout_s}s, force-killing.")

            ray.kill(replica, no_restart=True)
            ray.util.remove_placement_group(self._placement_group)
            self._state = ReplicaState.STOPPED
            return True
        return False

    def get_actor_handle(self):
        assert self._state == ReplicaState.RUNNING, (
            f"State must be {ReplicaState.RUNNING}, *not* {self._state}")
        return self._actor_handle


class BackendState:
    """Manages all state for backends in the system.

    This class is *not* thread safe, so any state-modifying methods should be
    called with a lock held.
    """

    def __init__(self, controller_name: str, detached: bool,
                 kv_store: RayInternalKVStore, long_poll_host: LongPollHost,
                 goal_manager: AsyncGoalManager):
        self._controller_name = controller_name
        self._detached = detached
        self._kv_store = kv_store
        self._long_poll_host = long_poll_host
        self._goal_manager = goal_manager

        self._replicas: Dict[BackendTag, Dict[ReplicaState, List[
            BackendReplica]]] = defaultdict(lambda: defaultdict(list))
        self._backend_metadata: Dict[BackendTag, BackendInfo] = dict()
        self._target_replicas: Dict[BackendTag, int] = defaultdict(int)
        self._target_versions: Dict[BackendTag, str] = dict()
        self.backend_goals: Dict[BackendTag, GoalId] = dict()

        # Un-checkpointed state.
        self.pending_goals: Dict[GoalId, asyncio.Event] = dict()

        checkpoint = self._kv_store.get(CHECKPOINT_KEY)
        if checkpoint is not None:
            (self._replicas, self._backend_metadata, self._target_replicas,
             self._target_versions, self.backend_goals,
             pending_goal_ids) = pickle.loads(checkpoint)

            for goal_id in pending_goal_ids:
                self._goal_manager.create_goal(goal_id)

        self._notify_backend_configs_changed()
        self._notify_replica_handles_changed()

    def _checkpoint(self) -> None:
        self._kv_store.put(
            CHECKPOINT_KEY,
            pickle.dumps(
                (self._replicas, self._backend_metadata, self._target_replicas,
                 self._target_versions, self.backend_goals,
                 self._goal_manager.get_pending_goal_ids())))

    def _notify_backend_configs_changed(self) -> None:
        self._long_poll_host.notify_changed(LongPollKey.BACKEND_CONFIGS,
                                            self.get_backend_configs())

    def get_running_replica_handles(
            self) -> Dict[BackendTag, Dict[ReplicaTag, ActorHandle]]:
        return {
            backend_tag: {
                backend_replica._replica_tag:
                backend_replica.get_actor_handle()
                for backend_replica in state_to_replica_dict[
                    ReplicaState.RUNNING]
            }
            for backend_tag, state_to_replica_dict in self._replicas.items()
        }

    def _notify_replica_handles_changed(self) -> None:
        self._long_poll_host.notify_changed(
            LongPollKey.REPLICA_HANDLES, {
                backend_tag: list(replica_dict.values())
                for backend_tag, replica_dict in
                self.get_running_replica_handles().items()
            })

    def get_backend_configs(self) -> Dict[BackendTag, BackendConfig]:
        return {
            tag: info.backend_config
            for tag, info in self._backend_metadata.items()
        }

    def get_backend(self, backend_tag: BackendTag) -> Optional[BackendInfo]:
        return self._backend_metadata.get(backend_tag)

    def _set_backend_goal(self, backend_tag: BackendTag,
                          backend_info: Optional[BackendInfo],
                          version: Optional[str]) -> None:
        existing_goal_id = self.backend_goals.get(backend_tag)
        new_goal_id = self._goal_manager.create_goal()

        if backend_info is not None:
            self._backend_metadata[backend_tag] = backend_info
            self._target_replicas[
                backend_tag] = backend_info.backend_config.num_replicas
        else:
            self._target_replicas[backend_tag] = 0

        self._target_versions[backend_tag] = version
        self.backend_goals[backend_tag] = new_goal_id

        return new_goal_id, existing_goal_id

    def create_backend(self,
                       backend_tag: BackendTag,
                       backend_config: BackendConfig,
                       replica_config: ReplicaConfig,
                       version: Optional[str] = None) -> Optional[GoalId]:
        # Ensures this method is idempotent.
        backend_info = self._backend_metadata.get(backend_tag)
        if backend_info is not None:
            # Old codepath.
            if version is None:
                if (backend_info.backend_config == backend_config
                        and backend_info.replica_config == replica_config):
                    return None
            # New codepath: treat version as ground truth for implementation.
            else:
                if (backend_info.backend_config == backend_config
                        and self._target_versions[backend_tag] == version):
                    return None

        backend_replica_class = create_backend_replica(
            replica_config.backend_def)

        # Save creator that starts replicas, the arguments to be passed in,
        # and the configuration for the backends.
        backend_info = BackendInfo(
            worker_class=backend_replica_class,
            backend_config=backend_config,
            replica_config=replica_config)

        new_goal_id, existing_goal_id = self._set_backend_goal(
            backend_tag, backend_info, version)

        # NOTE(edoakes): we must write a checkpoint before starting new
        # or pushing the updated config to avoid inconsistent state if we
        # crash while making the change.
        self._checkpoint()
        self._notify_backend_configs_changed()

        if existing_goal_id is not None:
            self._goal_manager.complete_goal(existing_goal_id)
        return new_goal_id

    def delete_backend(self, backend_tag: BackendTag,
                       force_kill: bool = False) -> Optional[GoalId]:
        # This method must be idempotent. We should validate that the
        # specified backend exists on the client.
        if backend_tag not in self._backend_metadata:
            return None

        new_goal_id, existing_goal_id = self._set_backend_goal(
            backend_tag, None, None)
        if force_kill:
            self._backend_metadata[
                backend_tag].backend_config.\
                    experimental_graceful_shutdown_timeout_s = 0

        self._checkpoint()
        if existing_goal_id is not None:
            self._goal_manager.complete_goal(existing_goal_id)
        return new_goal_id

    def update_backend_config(self, backend_tag: BackendTag,
                              config_options: BackendConfig):
        if backend_tag not in self._backend_metadata:
            raise ValueError(f"Backend {backend_tag} is not registered")

        stored_backend_config = self._backend_metadata[
            backend_tag].backend_config
        updated_config = stored_backend_config.copy(
            update=config_options.dict(exclude_unset=True))
        updated_config._validate_complete()
        self._backend_metadata[backend_tag].backend_config = updated_config

        new_goal_id, existing_goal_id = self._set_backend_goal(
            backend_tag, self._backend_metadata[backend_tag],
            self._target_versions[backend_tag])

        # NOTE(edoakes): we must write a checkpoint before pushing the
        # update to avoid inconsistent state if we crash after pushing the
        # update.
        self._checkpoint()
        if existing_goal_id is not None:
            self._goal_manager.complete_goal(existing_goal_id)

        # Inform the routers and backend replicas about config changes.
        # TODO(edoakes): this should only happen if we change something other
        # than num_replicas.
        self._notify_backend_configs_changed()

        return new_goal_id

    def _stop_wrong_version_replicas(
            self, backend_tag: BackendTag, version: str,
            graceful_shutdown_timeout_s: float) -> int:
        # TODO(edoakes): to implement rolling upgrade, all we should need to
        # do is cap the number of old version replicas that are stopped here.
        num_stopped = 0
        for target_state in [
                ReplicaState.SHOULD_START, ReplicaState.STARTING,
                ReplicaState.RUNNING
        ]:
            target_version = []
            wrong_version = []
            for replica in self._replicas[backend_tag][target_state]:
                if replica.version == version:
                    target_version.append(replica)
                else:
                    wrong_version.append(replica)

            self._replicas[backend_tag][target_state] = target_version
            for replica in wrong_version:
                replica.set_should_stop(graceful_shutdown_timeout_s)
                self._replicas[backend_tag][ReplicaState.SHOULD_STOP].append(
                    replica)
                num_stopped += 1

        if num_stopped > 0:
            logger.info(f"Stopping {num_stopped} replicas of backend "
                        f"'{backend_tag}' with outdated versions.")

    def _scale_backend_replicas(
            self,
            backend_tag: BackendTag,
            num_replicas: int,
            version: str,
    ) -> bool:
        """Scale the given backend to the number of replicas.

        NOTE: this does not actually start or stop the replicas, but instead
        adds them to ReplicaState.SHOULD_START or ReplicaState.SHOULD_STOP.
        The caller is responsible for then first writing a checkpoint and then
        actually starting/stopping the intended replicas. This avoids
        inconsistencies with starting/stopping a replica and then crashing
        before writing a checkpoint.
        """
        logger.debug("Scaling backend '{}' to {} replicas".format(
            backend_tag, num_replicas))
        assert (backend_tag in self._backend_metadata
                ), "Backend {} is not registered.".format(backend_tag)
        assert num_replicas >= 0, ("Number of replicas must be"
                                   " greater than or equal to 0.")

        backend_info: BackendInfo = self._backend_metadata[backend_tag]
        graceful_shutdown_timeout_s = (
            backend_info.backend_config.
            experimental_graceful_shutdown_timeout_s)

        self._stop_wrong_version_replicas(backend_tag, version,
                                          graceful_shutdown_timeout_s)

        current_num_replicas = sum([
            len(self._replicas[backend_tag][ReplicaState.SHOULD_START]),
            len(self._replicas[backend_tag][ReplicaState.STARTING]),
            len(self._replicas[backend_tag][ReplicaState.RUNNING]),
        ])

        delta_num_replicas = num_replicas - current_num_replicas

        if delta_num_replicas == 0:
            return False

        elif delta_num_replicas > 0:
            logger.debug("Adding {} replicas to backend {}".format(
                delta_num_replicas, backend_tag))
            for _ in range(delta_num_replicas):
                replica_tag = "{}#{}".format(backend_tag, get_random_letters())
                self._replicas[backend_tag][ReplicaState.SHOULD_START].append(
                    BackendReplica(self._controller_name, self._detached,
                                   replica_tag, backend_tag, version))

        elif delta_num_replicas < 0:
            logger.debug("Removing {} replicas from backend '{}'".format(
                -delta_num_replicas, backend_tag))
            assert self._target_replicas[backend_tag] >= delta_num_replicas

            for _ in range(-delta_num_replicas):
                replica_state_dict = self._replicas[backend_tag]
                list_to_use = replica_state_dict[ReplicaState.SHOULD_START] \
                    or replica_state_dict[ReplicaState.STARTING] \
                    or replica_state_dict[ReplicaState.RUNNING]

                assert len(list_to_use), replica_state_dict
                replica_to_stop = list_to_use.pop()

                replica_to_stop.set_should_stop(graceful_shutdown_timeout_s)
                self._replicas[backend_tag][ReplicaState.SHOULD_STOP].append(
                    replica_to_stop)

        return True

    def _scale_all_backends(self):
        checkpoint_needed = False
        for backend_tag, num_replicas in list(self._target_replicas.items()):
            checkpoint_needed |= self._scale_backend_replicas(
                backend_tag, num_replicas, self._target_versions[backend_tag])
            if num_replicas == 0:
                del self._backend_metadata[backend_tag]
                del self._target_replicas[backend_tag]
                del self._target_versions[backend_tag]

        if checkpoint_needed:
            self._checkpoint()

    def _pop_replicas_of_state(self, state: ReplicaState
                               ) -> List[Tuple[ReplicaState, BackendTag]]:
        replicas = []
        for backend_tag, state_to_replica_dict in self._replicas.items():
            if state in state_to_replica_dict:
                replicas.extend(
                    (replica, backend_tag)
                    for replica in state_to_replica_dict.pop(state))

        return replicas

    def _completed_goals(self) -> List[GoalId]:
        completed_goals = []
        all_tags = set(self._replicas.keys()).union(
            set(self._backend_metadata.keys()))

        for backend_tag in all_tags:
            desired_num_replicas = self._target_replicas.get(backend_tag)
            state_dict = self._replicas.get(backend_tag, {})
            existing_info = state_dict.get(ReplicaState.RUNNING, [])

            # If we have pending ops, the current goal is *not* ready
            if (state_dict.get(ReplicaState.SHOULD_START)
                    or state_dict.get(ReplicaState.STARTING)
                    or state_dict.get(ReplicaState.SHOULD_STOP)
                    or state_dict.get(ReplicaState.STOPPING)):
                continue

            # Check for deleting.
            if (not desired_num_replicas or
                    desired_num_replicas == 0) and \
                    (not existing_info or len(existing_info) == 0):
                completed_goals.append(
                    self.backend_goals.pop(backend_tag, None))

            # Check for a non-zero number of backends.
            if (desired_num_replicas and existing_info) \
                    and desired_num_replicas == len(existing_info):
                # Check that all running replicas are the target version.
                target_version = self._target_versions[backend_tag]
                if all(r.version == target_version for r in existing_info):
                    completed_goals.append(
                        self.backend_goals.pop(backend_tag, None))
        return [goal for goal in completed_goals if goal]

    def update(self) -> bool:
        """Updates the state of all running replicas to match the goal state.
        """
        self._scale_all_backends()

        for goal_id in self._completed_goals():
            self._goal_manager.complete_goal(goal_id)

        for replica_state, backend_tag in self._pop_replicas_of_state(
                ReplicaState.SHOULD_START):
            replica_state.start(self._backend_metadata[backend_tag])
            self._replicas[backend_tag][ReplicaState.STARTING].append(
                replica_state)

        for replica_state, backend_tag in self._pop_replicas_of_state(
                ReplicaState.SHOULD_STOP):
            replica_state.stop()
            self._replicas[backend_tag][ReplicaState.STOPPING].append(
                replica_state)

        transition_triggered = False

        for replica_state, backend_tag in self._pop_replicas_of_state(
                ReplicaState.STARTING):
            if replica_state.check_started():
                self._replicas[backend_tag][ReplicaState.RUNNING].append(
                    replica_state)
                transition_triggered = True
            else:
                self._replicas[backend_tag][ReplicaState.STARTING].append(
                    replica_state)

        for replica_state, backend_tag in self._pop_replicas_of_state(
                ReplicaState.STOPPING):
            if replica_state.check_stopped():
                transition_triggered = True
            else:
                self._replicas[backend_tag][ReplicaState.STOPPING].append(
                    replica_state)

        for backend_tag in list(self._replicas.keys()):
            if not any(self._replicas[backend_tag]):
                del self._replicas[backend_tag]
                del self._backend_metadata[backend_tag]
                del self._target_replicas[backend_tag]

        if transition_triggered:
            self._checkpoint()
            self._notify_replica_handles_changed()
