# Fixing "Not authorized to control networking"

If `aa-hmi run` fails at the WiFi-connect step with:

```
error: 'nmcli device wifi connect ...' exited 4: Error: Failed to add/activate new connection: Not authorized to control networking.
```

this is a **polkit permissions issue, not a bug**, and it's fixable
permanently in about 30 seconds — confirmed live (2026-09-18) on
Raspberry Pi OS/Debian Trixie.

## Why this happens

NetworkManager's default polkit rule
(`/usr/share/polkit-1/rules.d/*NetworkManager*.rules`) looks like:

```js
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.NetworkManager.settings.modify.system" &&
        subject.local && subject.active &&
        (subject.isInGroup("sudo") || subject.isInGroup("netdev"))) {
        return polkit.Result.YES;
    }
});
```

`subject.active` requires an "active" console/logind session (a real
local login, graphical or TTY). A plain SSH session — exactly how you'd
normally reach a headless Raspberry Pi — never has one, so this rule
denies it, **even if you're already in the `netdev` group** (check with
`groups $(whoami)`). Read-only `nmcli` commands (like `device wifi list`)
still work fine over SSH, which makes this confusing — only the
create/activate actions require `subject.active`.

Running `aa-hmi` with `sudo` works around this, but has its own gotcha:
if you installed with `pipx` (the README's recommended method), the
`aa-hmi` command lives in `~/.local/bin`, which is **not** on `sudo`'s
restricted `secure_path` — `sudo aa-hmi run` fails with `command not
found`. You'd need `sudo $(command -v aa-hmi) run` every time.

## The permanent fix: a local polkit rule

Add a rule that grants full NetworkManager control to `netdev` group
members regardless of session type (you're very likely already in that
group — `groups $(whoami)` to check; `sudo usermod -aG netdev $(whoami)`
and re-login if not):

```bash
sudo tee /etc/polkit-1/rules.d/51-aa-hmi-networkmanager.rules > /dev/null <<'EOF'
// Installed for aa-hmi: let members of the netdev group manage
// NetworkManager connections without requiring an "active" console/
// logind session (the stock rule requires subject.active, which a plain
// SSH session never has). See docs/networkmanager-permissions.md.
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 &&
        subject.isInGroup("netdev")) {
        return polkit.Result.YES;
    }
});
EOF
sudo systemctl restart polkit
```

Then `aa-hmi run` (plain, no `sudo`) should just work. Verified live: the
exact same `nmcli device wifi connect` call that failed without this
rule succeeded immediately after adding it, no logout/login or reboot
needed (polkit picks up rule changes on restart).

## Security note

This rule is intentionally broad — it grants **every**
`org.freedesktop.NetworkManager.*` action (not just the WiFi-connect one
`aa-hmi` needs) to **every** `netdev`-group member, from **any** session
type, permanently. On a shared or multi-user machine, consider scoping
this down (e.g. check `subject.user == "youruser"` instead of the group,
or list only the specific action IDs `aa-hmi` actually needs:
`org.freedesktop.NetworkManager.settings.modify.system` and
`org.freedesktop.NetworkManager.network-control`). On a single-user
Raspberry Pi dedicated to this project, the broad version above is a
reasonable, common tradeoff — this is the same kind of rule many
headless-Pi/kiosk setups already add for unrelated reasons.

## If you'd rather not change system policy

Use `sudo` per-run instead, referencing the full path so `sudo` can find
a `pipx`-installed command:

```bash
sudo $(command -v aa-hmi) run
```

This works but needs typing every time, and (already handled by
`aa-hmi` itself) requires the credential cache to be readable by both
your normal user and root — see `src/aa_hmi/cache.py`'s `$SUDO_UID`
chown handling, which takes care of this automatically.
