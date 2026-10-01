## ADDED Requirements

### Requirement: One remote conversation per CLI session
When remote communication is effective for a `feedback-cli` invocation, the system MUST create exactly one remote conversation bound to that invocation's session and MUST NOT reuse it for any other session.

#### Scenario: Single invocation
- **WHEN** remote communication is effective and Agent runs `feedback-cli`
- **THEN** system creates one remote conversation carrying the project directory, the summary, a short session identifier and the deadline
- **THEN** that conversation is used only for this session

#### Scenario: Three concurrent invocations
- **WHEN** three `feedback-cli` processes run concurrently with remote communication effective
- **THEN** each process creates and listens to its own remote conversation
- **THEN** a reply in one conversation is delivered only to the process that owns it

### Requirement: Effectiveness conditions and no-op default
Remote communication MUST be effective for an invocation only when it is enabled, its configuration is verified (the stored verification fingerprint matches the current configuration), and the configuration can be read at startup. Otherwise the system MUST behave exactly as without this feature and MUST NOT create any remote object or issue any network request to the remote provider.

#### Scenario: Disabled or unverified
- **WHEN** remote communication is disabled, was never verified, or its configuration changed after verification
- **THEN** the invocation runs local-only
- **THEN** no request is sent to the remote provider

#### Scenario: Configuration file missing
- **WHEN** the remote configuration file does not exist
- **THEN** the invocation runs local-only

### Requirement: Local window remains and arbitration is shared
The local window MUST be launched exactly as without remote communication. Local submission, remote reply and timeout MUST pass through the same first-success arbitration defined by `feedback-submission-arbitration`, so exactly one outcome is committed for the session.

#### Scenario: Remote reply first
- **WHEN** an authorized remote reply is accepted before any local submission
- **THEN** system commits the reply as session feedback with source `remote:<provider>`
- **THEN** CLI stops waiting and outputs the same MCP-compatible text as for a local submission
- **THEN** the local window is closed by the normal CLI shutdown path

#### Scenario: Local submission first
- **WHEN** the user submits locally before any remote reply
- **THEN** system ignores later remote replies
- **THEN** the remote conversation is finalized as answered locally

#### Scenario: Simultaneous submission
- **WHEN** a local submission and a remote reply arrive at nearly the same time
- **THEN** only the first successful commit is accepted and the other side observes a conflict
- **THEN** already committed feedback is never overwritten

### Requirement: Remote failures never affect the local flow
Remote operations MUST run as background work. Startup MUST NOT wait for the network, and any remote error MUST NOT block, fail, or alter the local session beyond a bounded timeout.

#### Scenario: Provider unreachable at startup
- **WHEN** the provider cannot be reached when the invocation starts
- **THEN** the local window opens without waiting for the remote attempt
- **THEN** the local window shows remote status as unavailable together with a reason
- **THEN** local submission and timeout behave as without remote communication

#### Scenario: Permanent authorization failure
- **WHEN** the provider rejects the credentials or permissions with a non-retryable error
- **THEN** the system stops contacting the provider for this session
- **THEN** remote status is shown as unavailable together with the reason

#### Scenario: Transient failure
- **WHEN** a network error or provider server error occurs while waiting for a reply
- **THEN** the system retries with bounded backoff and reports remote status as unavailable until recovery
- **THEN** after recovery the status returns to waiting for reply

### Requirement: Remote status visibility
The local window MUST display the current remote state: off, connecting, waiting for reply, unavailable (with reason), answered remotely, or answered locally.

#### Scenario: Waiting state
- **WHEN** the remote conversation was created successfully
- **THEN** the local window shows the waiting-for-reply state

#### Scenario: Remote state changes to unavailable
- **WHEN** the remote channel becomes unavailable during the wait
- **THEN** the local window updates to the unavailable state with the reason

### Requirement: Bounded finalization with outcome marking
On every session end path the system MUST attempt, within a bounded time, to mark the remote conversation with the final outcome (answered remotely, answered locally, timeout, interrupted, error) and to archive it. The bound MUST be longer when the session ended with a remote answer (about 20 seconds: the user is away from the computer and the connection may be slow) than for every other outcome (about 5 seconds: a local answer MUST NOT delay the CLI exit noticeably). A finalization failure MUST NOT change the CLI result or exit behavior.

#### Scenario: Timeout
- **WHEN** the CLI wait times out
- **THEN** the remote conversation is marked as timed out and archived

#### Scenario: Handled interruption
- **WHEN** the CLI receives an interruption that can be handled
- **THEN** the remote conversation is marked interrupted and archived before exit when time allows

#### Scenario: Finalization failure
- **WHEN** marking or archiving the remote conversation fails
- **THEN** the CLI still exits with the result it already determined

#### Scenario: Slow close after a remote answer
- **WHEN** the session ended with a remote answer and closing the conversation takes longer than the short bound but within the longer one
- **THEN** the outcome is still marked and the conversation archived before exit

#### Scenario: Slow close after a local answer
- **WHEN** the session ended with a local answer and closing the conversation takes longer than the short bound
- **THEN** the close is abandoned and the CLI exits with the result it already determined

### Requirement: Liveness marker for stale conversations
While waiting, the system MUST state the deadline in the remote conversation and MUST refresh a last-alive timestamp in it at most once per minute, so that conversations left behind by a forcibly terminated process are recognizable.

#### Scenario: Normal waiting
- **WHEN** the session is waiting for a reply
- **THEN** the conversation shows the deadline and a last-alive timestamp that advances about once per minute

#### Scenario: Forced termination
- **WHEN** the CLI process is killed without a chance to finalize
- **THEN** the conversation keeps its deadline and its last-alive timestamp stops advancing

### Requirement: Minimal provider interface
A remote provider MUST be driven through three operations only: open a conversation, wait for a reply (cancellable), and close the conversation with an outcome. Provider-specific behavior such as liveness refresh MUST stay inside the provider.

#### Scenario: Fake provider drives a full session
- **WHEN** a test provider implementing only the three operations is plugged in
- **THEN** a complete session (open, reply accepted, close) runs without any provider-specific code in the coordinator
