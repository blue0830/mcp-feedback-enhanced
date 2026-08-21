## ADDED Requirements

### Requirement: Registry entry reflects final bind target
The system MUST create or update instance registry data only after the server has successfully bound to its final host and port. Registry host/port SHALL match the actual listening endpoint used for routing.

#### Scenario: Port rebinding occurs at startup
- **WHEN** the preferred startup port is unavailable and the server binds to a different port
- **THEN** the registry entry SHALL contain the final bound port
- **AND** CLI route resolution SHALL use that final port

### Requirement: Registry lifecycle follows session lifecycle
Registry metadata linked to a session MUST be removed by all session cleanup paths, including synchronous cleanup, asynchronous cleanup, session replacement, memory-pressure cleanup, timeout cleanup, and service shutdown.

#### Scenario: Session cleaned synchronously
- **WHEN** a session is removed by a synchronous cleanup path
- **THEN** the related registry entry SHALL be deleted in the same cleanup flow

#### Scenario: Service shutdown
- **WHEN** the process shuts down gracefully
- **THEN** all active registry entries owned by that process SHALL be removed before exit

### Requirement: Stale cleanup uses persisted process identity
Registry records MUST include process identity fields (`pid` and process start time). Stale cleanup SHALL delete records only when process absence or identity mismatch is confirmed.

#### Scenario: PID reused by another process
- **WHEN** a registry record PID exists but its process start time differs from the recorded value
- **THEN** the registry record SHALL be treated as stale and eligible for deletion

#### Scenario: Process still alive
- **WHEN** PID and process start time both match a running process
- **THEN** stale cleanup SHALL keep the registry record
