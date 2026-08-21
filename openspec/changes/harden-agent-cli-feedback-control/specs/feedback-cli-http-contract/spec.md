## ADDED Requirements

### Requirement: Actionable list and full status semantics
The CLI HTTP contract MUST separate actionable discovery from lifecycle inspection: `list` SHALL return only sessions that are still submittable, while `status` SHALL return the full state for a specified session.

#### Scenario: List excludes completed sessions
- **WHEN** a session has already been committed or timed out
- **THEN** `list` SHALL NOT include that session

#### Scenario: Status returns completed session
- **WHEN** CLI queries `status` for a committed session that is not yet cleaned
- **THEN** the response SHALL include the committed state and final result information

### Requirement: Submit validation and error contract
Submit endpoints MUST validate required fields and enforce payload constraints before attempting commit. Validation failures SHALL return `400`, unknown sessions SHALL return `404`, and non-winning already-committed submissions SHALL return `409`.

#### Scenario: Missing required field
- **WHEN** submit payload omits a required field such as `session_id` or `feedback`
- **THEN** the endpoint SHALL return `400` with structured validation error details

#### Scenario: Feedback exceeds maximum length
- **WHEN** submit payload feedback length exceeds configured limit
- **THEN** the endpoint SHALL return `400`
- **AND** no state change SHALL be applied

### Requirement: Idempotent retry by request identifier
Submit endpoints MUST support idempotent retry using a request identifier so a retried request can return the original outcome when the first attempt succeeded but client-side timeout occurred.

#### Scenario: Retry after client timeout on successful first commit
- **WHEN** the first submit attempt commits successfully but caller times out before receiving response
- **AND** caller retries with the same request identifier
- **THEN** the endpoint SHALL return the original successful outcome
- **AND** SHALL NOT create a second commit

#### Scenario: Different request identifier after commit
- **WHEN** a new request identifier is used after a session is already committed
- **THEN** the endpoint SHALL return `409` conflict with committed-state information

### Requirement: Loopback-only exposure in unauthenticated mode
Without an authentication boundary, CLI-targeted HTTP submission endpoints MUST be exposed on loopback addresses only.

#### Scenario: Non-loopback host configured without auth
- **WHEN** host configuration resolves to a non-loopback address while auth is disabled
- **THEN** startup or route activation SHALL reject that configuration with a clear error
