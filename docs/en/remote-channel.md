# Remote Communication Guide (Discord)

`feedback-cli` can mirror every feedback request to a private Discord forum channel, so you can answer from your phone. The first usable reply from an allowed user ends the CLI exactly like a local submission. The local window still opens, and whichever side answers first wins.

It is **off by default** and only applies to `feedback-cli` (windows served by the MCP server never show it).

## 🧭 How It Works

- One `feedback-cli` call = one forum post. The title is `[project] first line of the summary (#shortId)`. The first message carries the summary, project directory, deadline and reply instructions, and mentions the allowed users so your phone gets a push notification.
- The **first usable text message** from an allowed user in that post is the final reply, and the bot confirms with a receipt right away. Text attachments count too (Discord turns long text into `message.txt`); images and other files are ignored.
- If you answer in the local window first, the post is marked "answered locally" and archived.
- Replies are fetched by polling Discord over HTTPS about every 3 seconds. There is no Gateway connection and no background service.
- The top bar of the local window shows the current remote status (off / connecting / waiting for reply / unavailable / answered remotely / answered locally). Click it to open the remote settings.

## 🛠️ Setup

### 1. Create the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. On the **Bot** tab, click **Reset Token** and copy the token (it is shown only once).
3. On the same tab, under **Privileged Gateway Intents**, turn on **Message Content Intent**. Without it Discord returns empty message text and replies cannot be read.

### 2. Prepare a private server and a forum channel

1. Use a private server you trust: summaries and replies are stored by Discord.
2. Create a **Forum** channel. If the forum requires a tag on new posts, create at least one tag (the bot uses the first one).
3. Turn on **Developer Mode** (Discord User Settings → Advanced).
4. Right-click the forum channel → **Copy Channel ID**. Right-click your own name → **Copy User ID**.

### 3. Invite the bot

1. In the Developer Portal open **OAuth2 → URL Generator**, select the scope `bot`, and grant these permissions:
   - View Channels
   - Send Messages (this is "Create Posts" in a forum)
   - Send Messages in Threads (this is "Send Messages in Posts")
   - Embed Links
   - Attach Files (long summaries are attached as `summary.md`)
   - Read Message History
   - Manage Threads (this is "Manage Posts"; used to archive finished posts)
2. Open the generated URL and add the bot to your private server. Make sure no channel-specific permission override hides the forum channel from the bot.

`Manage Threads` is the only optional permission: without it everything works, but finished posts stay open until Discord archives them automatically after 24 hours.

### 4. Configure and test

```bash
feedback-cli --remote-settings
```

This opens a window that contains only the remote communication card. No feedback request is created and nothing is sent to Discord except the connection test. Run it yourself in a foreground terminal; it is **for humans, not for Agent calls**. Close the window when you are done (when the browser fallback is used, press Ctrl+C instead, or let `--timeout` end it). The same card is also in the settings tab of a normal `feedback-cli` window.

1. Fill in **Bot Token**, **Forum channel ID** and **Allowed reply user IDs** (your own user ID; separate several IDs with commas or spaces).
2. Click **Test connection** (unsaved edits are saved first). A test post appears in the forum: reply with any text in it within 120 seconds.
3. When every step is green, switch on **Enable remote communication**.

Changing the token, the channel or the allowed users invalidates the verification and switches the feature off until the next successful test. The token is write-only: it is never shown again, and leaving the field empty keeps the stored one.

## 🌐 Network

- Outbound HTTPS only: `discord.com` (API) plus `cdn.discordapp.com` and `media.discordapp.net` (text attachment downloads).
- No public IP, no listening port and no IP or listen-address setting are needed.
- If your machine needs a proxy or an exit node (for example a Tailscale exit node), set it up at the system level. The standard proxy environment variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`) are honored.

## ⚠️ Limits

- The outer tool timeout of the Agent and the `feedback-cli` timeout (default 3600 s) still apply, and a sleeping or powered-off computer cannot receive replies. A post whose CLI was killed stays open; the "last alive" time in its first message stops updating.
- Text replies only. Text attachments are limited to 256 KiB in total and must be UTF-8.
- Only allowed users count. The reply becomes the instruction the Agent receives, so use a private server and keep the allowed list short.
- The remote settings can only be edited through a loopback address (`127.0.0.1`, `localhost` or `[::1]`), for example an SSH-forwarded `localhost` port. Windows opened through another address (such as `MCP_WEB_HOST=0.0.0.0` via a LAN IP) cannot change them.
- The token is stored in plain text in `~/.config/mcp-feedback-enhanced/remote_channel.json` (owner-only permissions on POSIX). To turn everything off, switch the card off or delete that file. Use the `MCP_FEEDBACK_REMOTE_CONFIG` environment variable to keep the file somewhere else.

## 🩺 Troubleshooting

| Message on the card | What to do |
|---|---|
| message text is unreadable | Turn on Message Content Intent (step 1) |
| channel not found | Check the channel ID and that the bot joined the server |
| that channel is not a forum channel | Use a Forum channel |
| the bot lacks permissions | Grant the permissions from step 3 for that channel |
| the token is invalid or was revoked | Reset the token in the Developer Portal and enter it again |
| network unreachable | Check your proxy or exit node |
| the forum requires a tag | Create at least one tag in the forum |
