## ADDED Requirements

### Requirement: Unified first-success arbitration
The system MUST route web submission, CLI submission, and timeout completion through one shared atomic arbitration path per session. Exactly one path SHALL commit the final feedback outcome for a session.

#### Scenario: CLI and web submit concurrently
- **WHEN** a CLI submit request and a web UI submit request target the same session at nearly the same time
- **THEN** only the first successful commit SHALL be accepted
- **AND** the non-winning path SHALL receive a conflict result and MUST NOT overwrite committed feedback

#### Scenario: Submit and timeout race
- **WHEN** timeout completion and submit attempt execute concurrently for the same session
- **THEN** whichever path first commits the state change SHALL win
- **AND** the losing path SHALL observe the committed final state without applying a second commit

### Requirement: Winner definition and state finality
The winner MUST be defined as the first request that passes validation and successfully commits outcome state. A request that arrives first but fails validation or commit MUST NOT reserve ownership.

#### Scenario: Earlier request fails validation
- **WHEN** an earlier request is received but rejected by validation
- **THEN** a later valid request SHALL still be eligible to win and commit

#### Scenario: Session already committed
- **WHEN** any submit path targets a session whose outcome is already committed
- **THEN** the system SHALL return an already-committed conflict response
- **AND** the existing committed feedback SHALL remain unchanged

### Requirement: Loser-side visibility
The system SHALL provide explicit loser feedback to non-winning submitters so callers and UI can distinguish conflict from transport failure.

#### Scenario: Web submit loses after popup is open
- **WHEN** the web UI submits after CLI has already committed the same session
- **THEN** the web UI SHALL display an explicit "already submitted by another agent" style result
- **AND** the page SHALL NOT silently overwrite local history as if it won
