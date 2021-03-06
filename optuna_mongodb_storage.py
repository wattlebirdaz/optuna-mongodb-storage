import datetime
from typing import (
    Any,
    Container,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
    Callable,
)

import optuna
from optuna import exceptions
from optuna.distributions import (
    BaseDistribution,
    distribution_to_json,
    json_to_distribution,
)
from optuna.storages import BaseStorage
from optuna.storages._heartbeat import BaseHeartbeat
from optuna.storages._base import DEFAULT_STUDY_NAME_PREFIX
from optuna.study import StudyDirection, StudySummary
from optuna.trial import FrozenTrial, TrialState
from pymongo import MongoClient

_logger = optuna.logging.get_logger(__name__)


_str_to_study_direction_map: Dict[str, StudyDirection] = {
    "maximize": StudyDirection.MAXIMIZE,
    "minimize": StudyDirection.MINIMIZE,
    "not_set": StudyDirection.NOT_SET,
}
_study_direction_to_str_map = {v: k for k, v in _str_to_study_direction_map.items()}
_str_to_trial_state_map: Dict[str, TrialState] = {
    "running": TrialState.RUNNING,
    "complete": TrialState.COMPLETE,
    "pruned": TrialState.PRUNED,
    "fail": TrialState.FAIL,
    "waiting": TrialState.WAITING,
}
_trial_state_to_str_map = {v: k for k, v in _str_to_trial_state_map.items()}


def _datetime_to_str(time):
    if time is None:
        return None
    return str(time)


def _str_to_datetime(dt_str):
    if dt_str is None:
        return None
    return datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S.%f")


class MongoDBStorage(BaseStorage, BaseHeartbeat):
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        heartbeat_interval: Optional[int] = None,
        grace_period: Optional[int] = None,
        failed_trial_callback: Optional[
            Callable[["optuna.Study", FrozenTrial], None]
        ] = None,
    ) -> None:
        self._client = MongoClient(host=host, port=port)
        self._mongodb = self._client.optuna_study_database
        self._study_table = self._mongodb.studies
        self._trial_table = self._mongodb.trials

        self.heartbeat_interval = heartbeat_interval
        self.grace_period = grace_period
        self.failed_trial_callback = failed_trial_callback

    def create_new_study(self, study_name: Optional[str] = None) -> int:
        if (
            study_name is not None
            and self._study_table.count_documents({"study_name": study_name}) != 0
        ):
            raise exceptions.DuplicatedStudyError

        study_id = self._study_table.count_documents({})

        if study_name is None:
            study_name = "{}{:010d}".format(DEFAULT_STUDY_NAME_PREFIX, study_id)

        default_study_record = {
            "study_name": study_name,
            "directions": [_study_direction_to_str_map[StudyDirection.NOT_SET]],
            "user_attrs": {},
            "system_attrs": {},
            "study_id": study_id,
            "deleted": False,
            "datetime_start": _datetime_to_str(datetime.datetime.now()),
        }

        self._study_table.insert_one(default_study_record)

        _logger.info("A new study created in MongoDB with name: {}".format(study_name))

        return study_id

    def _check_study_id(self, study_id: int) -> None:
        if (
            self._study_table.count_documents(
                {"$and": [{"study_id": study_id}, {"deleted": False}]}
            )
            != 1
        ):
            raise KeyError("study_id {} does not exist.".format(study_id))

    def delete_study(self, study_id: int) -> None:
        self._check_study_id(study_id)
        self._study_table.update_one(
            {"study_id": study_id}, {"$set": {"deleted": True}}
        )

    def set_study_user_attr(self, study_id: int, key: str, value: Any) -> None:
        self._check_study_id(study_id)
        study_record = self._get_study_record(study_id)
        study_record["user_attrs"][key] = value
        self._study_table.replace_one({"study_id": study_id}, study_record)

    def set_study_system_attr(self, study_id: int, key: str, value: Any) -> None:
        self._check_study_id(study_id)
        study_record = self._get_study_record(study_id)
        study_record["system_attrs"][key] = value
        self._study_table.replace_one({"study_id": study_id}, study_record)

    def set_study_directions(
        self, study_id: int, directions: Sequence[StudyDirection]
    ) -> None:
        directions = list(directions)
        self._check_study_id(study_id)
        study_record = self._get_study_record(study_id)
        current_directions = [
            _str_to_study_direction_map[d] for d in study_record["directions"]
        ]
        if (
            current_directions[0] != StudyDirection.NOT_SET
            and current_directions != directions
        ):
            raise ValueError(
                "Cannot overwrite study direction from {} to {}.".format(
                    current_directions, directions
                )
            )
        study_record["directions"] = [
            _study_direction_to_str_map[d] for d in directions
        ]
        self._study_table.replace_one({"study_id": study_id}, study_record)

    def _get_study_record(self, study_id: int) -> Dict[str, Any]:
        return self._study_table.find_one({"study_id": study_id})

    def _get_study_record_field(self, study_id: int, field: str) -> Any:
        self._check_study_id(study_id)
        return self._get_study_record(study_id)[field]

    def get_study_id_from_name(self, study_name: str) -> int:
        res = self._study_table.find_one({"study_name": study_name})
        if res is None:
            raise KeyError("No such study {}.", format(study_name))
        return res["study_id"]

    def get_study_name_from_id(self, study_id: int) -> str:
        return self._get_study_record_field(study_id, "study_name")

    def get_study_directions(self, study_id: int) -> List[StudyDirection]:
        return [
            _str_to_study_direction_map[d]
            for d in self._get_study_record_field(study_id, "directions")
        ]

    def get_study_user_attrs(self, study_id: int) -> Dict[str, Any]:
        return self._get_study_record_field(study_id, "user_attrs")

    def get_study_system_attrs(self, study_id: int) -> Dict[str, Any]:
        return self._get_study_record_field(study_id, "system_attrs")

    def _convert_study_record_to_summary(
        self, study_record: Dict[str, Any]
    ) -> StudySummary:
        return StudySummary(
            study_name=study_record["study_name"],
            direction=None,
            best_trial=None,
            user_attrs=study_record["user_attrs"],
            system_attrs=study_record["system_attrs"],
            n_trials=0,
            datetime_start=_str_to_datetime(study_record["datetime_start"]),
            study_id=study_record["study_id"],
            directions=[
                _str_to_study_direction_map[d] for d in study_record["directions"]
            ],
        )

    def get_all_study_summaries(self, include_best_trial: bool) -> List[StudySummary]:
        # TODO: n_trials, best_trial
        study_records = self._study_table.find({"deleted": False})
        study_summaries = [
            self._convert_study_record_to_summary(study_record)
            for study_record in study_records
        ]
        return study_summaries

    def _convert_frozen_trial_to_record(
        self, study_id: int, trial: FrozenTrial
    ) -> Dict[str, Any]:
        return {
            "study_id": study_id,
            "trial_id": trial._trial_id,
            "number": trial.number,
            "state": _trial_state_to_str_map[trial.state],
            "params": trial.params,
            "distributions": {
                k: distribution_to_json(v) for k, v in trial.distributions.items()
            },
            "user_attrs": trial.user_attrs,
            "system_attrs": trial.system_attrs,
            "values": trial.values,
            "intermediate_values": {
                str(k): v for k, v in trial.intermediate_values.items()
            },
            "datetime_start": _datetime_to_str(trial.datetime_start),
            "datetime_complete": _datetime_to_str(trial.datetime_complete),
            "heartbeat": None,
        }

    def create_new_trial(
        self, study_id: int, template_trial: Optional[FrozenTrial] = None
    ) -> int:
        if template_trial is None:
            default_trial_record = {
                "study_id": study_id,
                "trial_id": -1,
                "number": -1,
                "state": _trial_state_to_str_map[TrialState.RUNNING],
                "params": {},
                "distributions": {},
                "user_attrs": {},
                "system_attrs": {},
                "values": None,
                "intermediate_values": {},
                "datetime_start": _datetime_to_str(datetime.datetime.now()),
                "datetime_complete": None,
                "heartbeat": None,
            }
        else:
            default_trial_record = self._convert_frozen_trial_to_record(
                study_id, template_trial
            )
        self._check_study_id(study_id)
        trial_id = self._trial_table.count_documents({})
        trial_number = self._trial_table.count_documents({"study_id": study_id})
        default_trial_record["trial_id"] = trial_id
        default_trial_record["number"] = trial_number

        self._trial_table.insert_one(default_trial_record)

        return trial_id

    def set_trial_param(
        self,
        trial_id: int,
        param_name: str,
        param_value_internal: float,
        distribution: BaseDistribution,
    ) -> None:
        self._check_trial_id(trial_id)
        trial_record = self._get_trial_record(trial_id)
        self.check_trial_is_updatable(
            trial_id, _str_to_trial_state_map[trial_record["state"]]
        )

        trial_record["params"][param_name] = distribution.to_external_repr(
            param_value_internal
        )
        trial_record["distributions"][param_name] = distribution_to_json(distribution)

        self._trial_table.replace_one({"trial_id": trial_id}, trial_record)

    def get_trial_id_from_study_id_trial_number(
        self, study_id: int, trial_number: int
    ) -> int:
        trial_record = self._trial_table.find_one(
            {"$and": [{"study_id": study_id}, {"number": trial_number}]}
        )
        if trial_record is None:
            raise KeyError(
                "No trial with trial number {} exists in study with study_id {}.".format(
                    trial_number, study_id
                )
            )
        return trial_record["trial_id"]

    def get_trial_number_from_id(self, trial_id: int) -> int:
        self._check_trial_id(trial_id)
        return self._get_trial_record_field(trial_id, "number")

    def get_trial_param(self, trial_id: int, param_name: str) -> float:
        self._check_trial_id(trial_id)
        return self._get_trial_record_field(trial_id, "param")

    def set_trial_state_values(
        self, trial_id: int, state: TrialState, values: Optional[Sequence[float]] = None
    ) -> bool:

        self._check_trial_id(trial_id)
        trial_record = self._get_trial_record(trial_id)
        current_state = _str_to_trial_state_map[trial_record["state"]]
        self.check_trial_is_updatable(trial_id, current_state)

        if current_state == state and state == TrialState.RUNNING:
            return False
        else:
            trial_record["state"] = _trial_state_to_str_map[state]
            trial_record["values"] = values
            self._trial_table.replace_one({"trial_id": trial_id}, trial_record)
            return True

    def set_trial_intermediate_value(
        self, trial_id: int, step: int, intermediate_value: float
    ) -> None:
        self._check_trial_id(trial_id)
        trial_record = self._get_trial_record(trial_id)
        self.check_trial_is_updatable(
            trial_id, _str_to_trial_state_map[trial_record["state"]]
        )
        trial_record["intermediate_values"][str(step)] = intermediate_value
        self._trial_table.replace_one({"trial_id": trial_id}, trial_record)

    def set_trial_user_attr(self, trial_id: int, key: str, value: Any) -> None:
        raise NotImplementedError

    def set_trial_system_attr(self, trial_id: int, key: str, value: Any) -> None:
        raise NotImplementedError

    def _check_trial_id(self, trial_id: int) -> None:
        if self._trial_table.count_documents({"trial_id": trial_id}) != 1:
            raise KeyError("trial_id {} does not exist.".format(trial_id))

    def _get_trial_record(self, trial_id: int) -> Dict[str, Any]:
        return self._trial_table.find_one({"trial_id": trial_id})

    def _get_trial_record_field(self, trial_id: int, field: str) -> Any:
        return self._get_trial_record(trial_id)[field]

    def _convert_record_to_frozen_trial(
        self, trial_record: Dict[str, Any]
    ) -> FrozenTrial:
        value: Optional[float]
        values: Optional[List[float]]
        if trial_record["values"] is None:
            value = None
            values = None
        elif len(trial_record["values"]) == 1:
            value = trial_record["values"][0]
            values = None
        else:
            value = None
            values = trial_record["values"]

        return FrozenTrial(
            trial_id=trial_record["trial_id"],
            number=trial_record["number"],
            state=_str_to_trial_state_map[trial_record["state"]],
            params=trial_record["params"],
            distributions={
                k: json_to_distribution(v)
                for k, v in trial_record["distributions"].items()
            },
            user_attrs=trial_record["user_attrs"],
            system_attrs=trial_record["system_attrs"],
            value=value,
            values=values,
            intermediate_values={
                int(key): value
                for key, value in trial_record["intermediate_values"].items()
            },
            datetime_start=_str_to_datetime(trial_record["datetime_start"]),
            datetime_complete=_str_to_datetime(trial_record["datetime_complete"]),
        )

    def get_trial(self, trial_id: int) -> FrozenTrial:
        self._check_trial_id(trial_id)
        trial_record = self._get_trial_record(trial_id)
        return self._convert_record_to_frozen_trial(trial_record)

    def get_all_trials(
        self,
        study_id: int,
        deepcopy: bool = True,
        states: Optional[Container[TrialState]] = None,
    ) -> List[FrozenTrial]:

        if states is None:
            trial_records = self._trial_table.find({"study_id": study_id})
        elif len(states) == 0:
            return []
        else:
            if len(states) == 1:
                cond = {"state": _trial_state_to_str_map[states[0]]}
            else:
                cond = {"$or": [{"state": _trial_state_to_str_map[s]} for s in states]}
            trial_records = self._trial_table.find(
                {"$and": [{"study_id": study_id}, cond]}
            )
        trials = [self._convert_record_to_frozen_trial(t) for t in trial_records]
        return trials

    def get_n_trials(
        self,
        study_id: int,
        state: Optional[Union[Tuple[TrialState, ...], TrialState]] = None,
    ) -> int:
        raise NotImplementedError

    def get_best_trial(self, study_id: int) -> FrozenTrial:
        return super().get_best_trial(study_id)

    def get_trial_params(self, trial_id: int) -> Dict[str, Any]:
        self._check_trial_id(trial_id)
        return self._get_trial_record_field(trial_id, "params")

    def get_trial_user_attrs(self, trial_id: int) -> Dict[str, Any]:
        self._check_trial_id(trial_id)
        return self._get_trial_record_field(trial_id, "user_attrs")

    def get_trial_system_attrs(self, trial_id: int) -> Dict[str, Any]:
        self._check_trial_id(trial_id)
        return self._get_trial_record_field(trial_id, "system_attrs")

    def read_trials_from_remote_storage(self, study_id: int) -> None:
        self._check_study_id(study_id)

    def remove_session(self) -> None:
        pass
        # raise NotImplementedError

    def check_trial_is_updatable(self, trial_id: int, trial_state: TrialState) -> None:
        if trial_state.is_finished():
            trial_record = self._get_trial_record(trial_id)
            raise RuntimeError(
                "Trial#{} has already finished and can not be updated.".format(
                    trial_record["number"]
                )
            )

    def is_heartbeat_enabled(self) -> bool:
        return super().is_heartbeat_enabled()

    def record_heartbeat(self, trial_id: int) -> None:
        self._check_trial_id(trial_id)
        trial_record = self._get_trial_record(trial_id)
        trial_record["heartbeat"] = self._trial_table.command("serverStatus")[
            "localTime"
        ]
        self._trial_table.replace_one({"trial_id": trial_id}, trial_record)

    def _get_stale_trial_ids(self, study_id: int) -> List[int]:
        assert self.get_heartbeat_interval() is not None

        if self.grace_period is None:
            grace_period = 2 * self.heartbeat_interval
        else:
            grace_period = self.grace_period

        stale_trial_ids = []

        running_trial_records = self._trial_table.find(
            {
                "$and": [
                    {"study_id": study_id},
                    {"state": _trial_state_to_str_map[TrialState.RUNNING]},
                ]
            }
        )

        current_time = self._trial_table.command("serverStatus")["localTime"]

        for trial_record in running_trial_records:
            last_heartbeat = trial_record["heartbeat"]
            if (current_time - last_heartbeat).total_seconds() > grace_period:
                stale_trial_ids.append(trial_record["trial_id"])

        return stale_trial_ids

    def get_heartbeat_interval(self) -> Optional[int]:
        return self.heartbeat_interval

    def get_failed_trial_callback(
        self,
    ) -> Optional[Callable[["optuna.Study", FrozenTrial], None]]:
        return self.failed_trial_callback
