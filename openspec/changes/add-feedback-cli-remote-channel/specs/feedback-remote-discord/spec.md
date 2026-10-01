## ADDED Requirements

### Requirement: One forum post per session
The Discord provider MUST create one post in the configured forum channel per session. The title MUST follow `[<project dir name>] <summary first line> (#<short session id>)` truncated to 100 characters. The first message MUST carry the summary, project directory, short session id, deadline and reply instructions.

#### Scenario: Post creation
- **WHEN** the provider opens a conversation for a session
- **THEN** it creates a forum post with the title format above and a first message containing the summary, project directory, short session id, deadline and instructions stating that the first message in the post is the final reply
- **THEN** the post is created with an auto-archive duration of 1440 minutes

#### Scenario: Summary exceeds embed capacity
- **WHEN** the summary is longer than 4000 characters
- **THEN** the embed shows the first 4000 characters with a truncation note
- **THEN** the full summary is attached to the same message as `summary.md`

### Requirement: Restricted mentions
The first message MUST mention each allowlisted user so that a push notification is delivered, and the message MUST restrict parsed mentions to the allowlisted users only.

#### Scenario: Summary contains broad mentions
- **WHEN** the summary text contains `@everyone` or role mentions
- **THEN** those mentions are not parsed and notify nobody

### Requirement: HTTP polling without Gateway or resident process
The provider MUST receive replies by polling the post's messages over HTTP with a cursor, and MUST NOT open a Discord Gateway connection or depend on any resident background process.

#### Scenario: Polling cursor
- **WHEN** the provider polls for replies
- **THEN** it requests messages after the last seen message id, starting from the id of the first message, at an interval of about 3 seconds with jitter
- **THEN** messages with ids not greater than the cursor are never processed again

#### Scenario: Concurrent processes
- **WHEN** several CLI processes poll their own posts at the same time
- **THEN** no process depends on or interferes with another

### Requirement: Authorized reply recognition
The provider MUST accept a message as the final reply only when its author is in the allowlist, the author is not a bot, the message is a normal message or a reply, and its usable text is non-empty. Usable text is the trimmed message text followed by the content of its text attachments, joined by a blank line. All other messages MUST be ignored without ending the session.

#### Scenario: First authorized message
- **WHEN** an allowlisted user posts a text message in the post
- **THEN** the provider immediately replies with a receipt message
- **THEN** the usable text is submitted as the final reply of the session

#### Scenario: Unauthorized author
- **WHEN** a user who is not in the allowlist posts in the post
- **THEN** the message is ignored and the session keeps waiting

#### Scenario: No usable text
- **WHEN** an allowlisted user posts a message without usable text (empty, image only, or only unreadable attachments)
- **THEN** the provider replies with a hint that only text (message text or text attachments) is supported and that message content must be readable
- **THEN** the session keeps waiting

#### Scenario: Text with non-text attachment
- **WHEN** an allowlisted user posts text together with an image or another non-text attachment
- **THEN** the text is submitted as the final reply
- **THEN** the receipt states that some attachments were ignored

### Requirement: Text attachment replies
The provider MUST read the text attachments of an authorized message as part of the reply. An attachment is a text attachment when its media type starts with `text/` or its file name ends with `.txt` or `.md`. The provider MUST download it only over https from `cdn.discordapp.com` or `media.discordapp.net` without following redirects to other hosts, MUST NOT send the bot token with the download, MUST limit the total attachment size to 256 KiB (checking the declared size first and the bytes actually read), and MUST require valid UTF-8 (an optional BOM is allowed) without NUL bytes. A message whose attachments cannot be read MUST be treated as unusable: the provider posts a hint naming the reason, keeps waiting, and does not retry the download. A failed download, including a 401 or 403 from the CDN, MUST NOT be treated as a credential failure of the session.

#### Scenario: Long reply sent as a file
- **WHEN** an allowlisted user posts a long reply that Discord turned into a `message.txt` attachment
- **THEN** the provider reads the attachment text, replies with the receipt, and submits the text as the final reply

#### Scenario: Text plus text attachment
- **WHEN** an allowlisted user posts text together with a text attachment
- **THEN** the submitted reply is the message text followed by the attachment text, separated by a blank line

#### Scenario: Attachment too large
- **WHEN** the attachment exceeds 256 KiB
- **THEN** the provider does not download it in full, posts a hint that the attachment is too large, and keeps waiting

#### Scenario: Not valid text
- **WHEN** the attachment is not valid UTF-8 or contains NUL bytes
- **THEN** the provider posts a hint that only UTF-8 text is supported and keeps waiting

#### Scenario: Unexpected host
- **WHEN** an attachment URL is not served from the allowed Discord CDN hosts
- **THEN** the attachment is not downloaded and the message is treated as unusable

#### Scenario: Download failure
- **WHEN** the download fails, times out, or the CDN answers 401 or 403
- **THEN** the provider posts a hint asking the user to resend, keeps waiting, and does not stop polling for the session

### Requirement: Rate limit and error handling
The provider MUST honor rate limit response headers and `Retry-After`, MUST use a connect timeout of 5 seconds and a total timeout of 10 seconds per request, MUST read proxy settings from the environment, and MUST stop all requests for the session after a Discord API request returns 401 or 403. Attachment downloads from the CDN are not Discord API requests and are governed by the text attachment requirement.

#### Scenario: Rate limited
- **WHEN** a Discord API request returns 429
- **THEN** the provider waits for the indicated retry interval before the next request

#### Scenario: Credentials rejected
- **WHEN** a Discord API request returns 401 or 403
- **THEN** the provider stops polling for this session and reports a permanent failure with the reason

#### Scenario: Server or network error
- **WHEN** a Discord API request fails with a network error or a 5xx response
- **THEN** the provider retries with exponential backoff capped at 30 seconds

### Requirement: Outcome marking and archive
On close, the provider MUST edit the first message to show the final outcome, MUST post a short explanation message when the outcome is not a remote reply, and MUST archive the post. It MUST NOT depend on sending to an archived post.

#### Scenario: Answered remotely
- **WHEN** the session ended with a remote reply
- **THEN** the first message shows the answered-remotely outcome and the post is archived

#### Scenario: Answered locally
- **WHEN** the session ended with a local submission
- **THEN** the first message shows the answered-locally outcome, a short note is posted, and the post is archived

#### Scenario: Timeout
- **WHEN** the session ended by timeout
- **THEN** the first message shows the timeout outcome and the post is archived

### Requirement: Liveness refresh in the first message
While waiting, the provider MUST edit the first message about once per minute to update the last-alive timestamp, and MUST ignore edit failures.

#### Scenario: Edit failure
- **WHEN** the last-alive edit fails
- **THEN** the provider continues polling and does not report a failure

### Requirement: Credential hygiene
The provider MUST NOT write the bot token to logs, error messages, UI status or any API response.

#### Scenario: Failure with token in request context
- **WHEN** a request fails and an error is logged or displayed
- **THEN** the output contains the failure reason but never the token value
