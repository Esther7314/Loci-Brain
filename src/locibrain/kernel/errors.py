class LociError(Exception):
    code = "loci_error"


class ConfigError(LociError):
    code = "config_error"


class CapabilityLoadError(LociError):
    code = "capability_load_error"


class PolicyViolation(LociError):
    code = "policy_violation"


class ClusterUnavailable(LociError):
    code = "cluster_unavailable"


class NotLeader(LociError):
    code = "not_leader"


class QuorumTimeout(LociError):
    code = "quorum_timeout"


class LogIntegrityError(LociError):
    code = "log_integrity_error"


class SnapshotRestoreError(LociError):
    code = "snapshot_restore_error"


class VectorRebuildError(LociError):
    code = "vector_rebuild_error"


class HotUpdateRejected(LociError):
    code = "hot_update_rejected"


class MigrationFailed(LociError):
    code = "migration_failed"
