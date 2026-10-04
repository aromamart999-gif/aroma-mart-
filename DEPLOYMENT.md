# Public launch setup

This folder contains a Docker Compose deployment scaffold with Waitress serving the Python app and Caddy as the public HTTPS reverse proxy. The shop app is not directly published on a host port; only Caddy accepts public web traffic. SQLite data, uploaded product photos, and Caddy certificates use persistent Docker volumes.

## Before launch

You need:

1. A domain name you control.
2. A Linux VPS with a public IP address and Docker Engine plus the Docker Compose plugin.
3. Permission to open ports 80 and 443 in the VPS firewall and provider firewall.

Point the domain's DNS A record to the VPS public IPv4 address. Add an AAAA record only if the VPS is reachable over IPv6. Caddy can obtain and renew HTTPS certificates after public DNS points to the server and the required ports are reachable. See the [Caddy HTTPS requirements](https://caddyserver.com/docs/automatic-https).

## First setup on the VPS

Copy this project folder to the VPS. In the folder containing `compose.yaml`:

1. Copy `.env.example` to `.env` and replace `shop.example.com` with your real domain in both entries. Keep `.env` private.
2. Create the production owner account and database:

   ```sh
   docker compose run --rm app python server.py setup-admin
   ```

   Choose a strong, private password when prompted. This production database starts separately from the local computer's database and is seeded with the current starter products and shoe rack listing.

3. Start the website:

   ```sh
   docker compose up -d --build
   ```

4. Visit `https://your-real-domain/` and sign in at `https://your-real-domain/admin`.

The app listens only inside the private Compose network; do not add a public port mapping for 4173. Caddy terminates HTTPS and forwards requests to Waitress. The app sets secure admin cookies when `AMORA_COOKIE_SECURE=1`.

## Before accepting real customer orders

- Replace or remove every item marked **Demo sample**.
- Complete the business contact details and legal, shipping, return, and privacy policy text in the shop.
- Set the real WhatsApp support number in Admin → Settings, or leave it blank.
- Remember that online payment is disabled; checkout accepts COD only.
- Back up the `amora_data` and `amora_uploads` Docker volumes securely. `docker compose down` preserves them; do not use `docker compose down -v` unless you intend to erase the store data.

Do not commit or upload a local `data/amora-mart.sqlite3` file to a public repository. It contains private admin/order data. Docker Compose named volumes keep the production database outside the image build context.

## Deployment validation

This is a self-hosted small-shop setup. It still needs a security review and deployment checks on the chosen VPS before handling real customer information. In particular, verify DNS, HTTPS, firewall rules, persistent-volume backups and recovery, admin sign-in, a test COD order, and the owner policies/contact details. Do not expose port 4173 directly to the internet.
