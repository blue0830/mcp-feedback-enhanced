# Remote Communication Guide (Discord)

`feedback-cli` can mirror every feedback request to a private Discord channel (an ordinary text channel or a forum channel), so you can answer from your phone. The first usable reply from an allowed user ends the CLI exactly like a local submission. The local window still opens, and whichever side answers first wins.

It is **off by default** and only applies to `feedback-cli` (windows served by the MCP server never show it).

## 🧭 How It Works

- One `feedback-cli` call = one thread. In a text channel the bot opens a thread; in a forum channel it creates a post (a forum post is a thread too). The title is `[project] first line of the summary (#shortId)`. The first message in it carries the summary, project directory, deadline and reply instructions, and mentions the allowed users so your phone gets a push notification.
- The **first usable text message** from an allowed user in that thread is the final reply, and the bot confirms with a receipt right away. Text attachments count too (Discord turns long text into `message.txt`); images and other files are ignored.
- If you answer in the local window first, the thread is marked "answered locally" and archived. A thread that was answered, timed out or interrupted is archived as well.
- Replies are fetched by polling Discord over HTTPS about every 3 seconds. There is no Gateway connection and no background service.
- The top bar of the local window shows the current remote status (off / connecting / waiting for reply / unavailable / answered remotely / answered locally). Click it to open the remote settings.

## 🛠️ Setup

### 1. Create the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. On the **Bot** tab, click **Reset Token** and copy the token (it is shown only once).
3. On the same tab, under **Privileged Gateway Intents**, turn on **Message Content Intent**. Without it Discord returns empty message text and replies cannot be read.

### 2. Prepare a private server and a private channel

1. Use a private server you trust: summaries and replies are stored by Discord.
2. Create **one** of these channels:
   - **Text channel** (works on every server): each request opens a thread inside it.
   - **Forum channel** (the server must be a Community server: Server Settings → Enable Community): each request becomes a post. If the forum requires a tag on new posts, create at least one tag (the bot uses the first one).
3. Make the channel **private** (Edit Channel → Permissions → Private Channel) and add the bot to it. The threads the bot opens in a text channel are public, so everybody who can see the channel can read them; a private channel keeps them private. A channel hidden from the bot cannot be used.
4. Turn on **Developer Mode** (Discord User Settings → Advanced).
5. Right-click the channel → **Copy Channel ID**. Right-click your own name → **Copy User ID**.

### 3. Invite the bot

1. In the Developer Portal open **OAuth2 → URL Generator**, select the scope `bot`, and grant these permissions:
   - View Channels
   - Send Messages (this is "Create Posts" in a forum)
   - Create Public Threads (text channels; not needed for a forum)
   - Send Messages in Threads (this is "Send Messages in Posts")
   - Embed Links
   - Attach Files (long summaries are attached as `summary.md`)
   - Read Message History
2. Open the generated URL and add the bot to your private server. Make sure the channel's permission overrides let the bot see it and use the permissions above.

`Manage Threads` is not needed: the bot archives the threads it created itself.

### 4. Configure and test

```bash
feedback-cli --remote-settings
```

This opens a window that contains only the remote communication card. No feedback request is created and nothing is sent to Discord except the connection test. Run it yourself in a foreground terminal; it is **for humans, not for Agent calls**. Close the window when you are done (when the browser fallback is used, press Ctrl+C instead, or let `--timeout` end it). The same card is also in the settings tab of a normal `feedback-cli` window.

1. Fill in **Bot Token**, **Channel ID** and **Allowed reply user IDs** (your own user ID; separate several IDs with commas or spaces).
2. Click **Test connection** (unsaved edits are saved first). A test thread (or post) appears in the channel: reply with any text in it within 120 seconds. A step that fails because of a flaky connection is repeated up to three times before it is reported.
3. When every step is green, switch on **Enable remote communication**.

Changing the token, the channel or the allowed users invalidates the verification and switches the feature off until the next successful test. The token is write-only: it is never shown again, and leaving the field empty keeps the stored one.

## 🌐 Network

- Outbound HTTPS only: `discord.com` (API) plus `cdn.discordapp.com` and `media.discordapp.net` (text attachment downloads).
- No public IP, no listening port and no IP or listen-address setting are needed.
- If your machine needs a proxy or an exit node (for example a Tailscale exit node), set it up at the system level. The standard proxy environment variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`) are honored.
- Slow or flaky connections are tolerated: a request may take up to 30 seconds. After a reply from Discord, `feedback-cli` may keep running for up to 20 seconds to mark the thread as answered and archive it. After a local answer it waits at most 5 seconds for that, so the Agent is not held up.

## ⚠️ Limits

- The outer tool timeout of the Agent and the `feedback-cli` timeout (default 3600 s) still apply, and a sleeping or powered-off computer cannot receive replies. A thread whose CLI was killed stays open; the "last alive" time in its first message stops updating.
- Every request leaves one archived thread behind; the bot never deletes it (that needs `Manage Threads`), so delete old ones in Discord whenever you like. Archived threads do not count against Discord's limit on active threads.
- Text replies only. Text attachments are limited to 256 KiB in total and must be UTF-8.
- Only allowed users count. The reply becomes the instruction the Agent receives, so use a private server and keep the allowed list short.
- The remote settings can only be edited through a loopback address (`127.0.0.1`, `localhost` or `[::1]`), for example an SSH-forwarded `localhost` port. Windows opened through another address (such as `MCP_WEB_HOST=0.0.0.0` via a LAN IP) cannot change them.
- The token is stored in plain text in `~/.config/mcp-feedback-enhanced/remote_channel.json` (owner-only permissions on POSIX). To turn everything off, switch the card off or delete that file. Use the `MCP_FEEDBACK_REMOTE_CONFIG` environment variable to keep the file somewhere else.

## 🩺 Troubleshooting

| Message on the card | What to do |
|---|---|
| message text is unreadable | Turn on Message Content Intent (step 1) |
| channel not found | Check the channel ID and that the bot joined the server |
| that channel is neither a text channel nor a forum channel | Use a text channel or a forum channel (not a voice channel, a category or a thread) |
| the bot lacks permissions | Grant the permissions from step 3 for that channel |
| the token is invalid or was revoked | Reset the token in the Developer Portal and enter it again |
| network unreachable | Check your proxy or exit node |
| the forum requires a tag | Create at least one tag in the forum |
