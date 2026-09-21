# Tailscale Setup Steps

Join each laptop to the GnomeServer tailnet.

## Steps

1. **Server:** install Tailscale
   ```
   curl -fsSL https://tailscale.com/install.sh | sh
   ```

2. **User:** install Tailscale on your machine (same command)

3. **User:** authenticate
   ```
   sudo tailscale up
   ```

4. **User:** follow the link in the browser and validate credentials with GitHub using email: `gnomeserver@proton.me`. Password: **[REDACTED - do not store in this file]**

5. **User:** click **Authorize** to connect your device to GnomeServer

6. Once the page and terminal show success, connect to the server:
   ```
   ssh df-server@server-debian
   ```

## Verify

```
tailscale status
tailscale ping server-debian
```
