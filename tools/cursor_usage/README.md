# Cursor usage token pusher

This macOS helper reads Cursor's current session token and cached account identity
from its local state database and sends them to the local-only Home Assistant webhook
used by `CursorUsageMonitor`. It runs once when loaded and every six hours thereafter.
The monitor routes each token to that account's own MQTT device and sensors, so
switching Cursor logins does not overwrite another account's usage.

The helper does not modify Cursor's database, store another local copy of the token,
or put the token in either repository.

AppDaemon validates the token received by the webhook and stores it at
`/data/cursor/session_token`. The add-on's private `/data` directory persists across
restarts and upgrades without placing the runtime credential in Home Assistant's
shared configuration or the AppDaemon repository.

## Install

Cursor must be installed and signed in. The Home Assistant webhook must also be
deployed before setup, because loading the launch agent triggers its first push.
`just check` prints the email address and display name associated with the token
without sending it.

```shell
cd tools/cursor_usage
just check
just setup
```

The default webhook URL is:

```text
http://homeassistant.local:8123/api/webhook/cursor_session_token
```

Pass a different URL when required:

```shell
just setup "https://home.example.com/api/webhook/cursor_session_token"
```

Setup installs:

- `~/.local/bin/cursor-token-push.py`
- `~/Library/LaunchAgents/com.worgarside.cursor-usage-token-push.plist`
- stdout and stderr logs under `~/Library/Logs/`

Run `just setup` again after this source script changes. Setup replaces the installed
copy and reloads the launch agent.

Check whether the agent is installed and loaded:

```shell
just status
```

## Uninstall

```shell
just teardown
```

Teardown unloads the launch agent and removes the installed script, property list,
and both log files. The source files in this repository are left untouched.
