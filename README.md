# AMORA MART

A mobile-first reseller storefront with a private owner dashboard. On a computer, it uses SQLite locally; on Vercel, it connects to a persistent Turso database. Supplier fulfilment is manual: use the admin copy buttons, place the corresponding supplier order yourself, then update its status here.

The customer-facing entry page and branded not-found page are in `public/index.html` and `public/404.html`. The Python app serves the storefront, admin route, and API; customer records and admin controls remain behind the authenticated backend.

## Run it on your computer

1. Open PowerShell in this folder.
2. Create your private admin login:

   ```powershell
   py server.py setup-admin
   ```

   Enter a username and a password of at least 12 characters. There is no default admin password.

3. Start the shop:

   ```powershell
   py server.py
   ```

4. Open [http://127.0.0.1:4173](http://127.0.0.1:4173). Use [http://127.0.0.1:4173/admin](http://127.0.0.1:4173/admin) to sign in and manage orders, products, images, categories, delivery pricing, and support settings.

To change the admin password later, run `py server.py setup-admin` again and use the same username.

## Your first real product

The shoe rack from your three supplied screenshots is in the catalogue as **Large-Capacity Roll-Up Door Shoe Rack**, priced at **₹847**. The product page has all three supplied photos. MRP is also ₹847, so the page does not imply a discount. Supplier cost is blank/unknown; the admin dashboard does not estimate a profit until you enter that cost.

The other starter listings are marked **Demo sample** in the shop and in Admin → Products. Hide or replace them before launch. They have no customer ratings or review counts.

## Data and owner access

- SQLite order and catalogue data: `data/amora-mart.sqlite3`
- Uploaded product photos: `uploads/`
- Admin passwords are stored as salted PBKDF2 hashes. The password is not included in browser code.
- Admin order, customer, supplier-cost, and settings APIs require an authenticated admin session. Admin write actions also require a CSRF token.
- Customers can retrieve their order history from the browser that placed them using a random, private order-access token. They can also track an order from another device with the order ID and checkout mobile number; that lookup shows order progress and item details, but not the delivery address or full phone number.
- Customer details are not sent in public product responses. Protect the computer and the database backups because the SQLite file contains customer addresses and phone numbers.
- Copy the database and `uploads/` folder to a secure backup location regularly.

## Before publishing publicly

This project includes a Vercel configuration. The Python API and `/admin` login work only when the full project folder is deployed, a persistent Turso database is connected, and the initial admin is created. Static HTML hosting by itself cannot save orders or authenticate an admin.

Follow [VERCEL-SETUP.md](VERCEL-SETUP.md) to configure Vercel, connect the permanent database, create your private admin login in the browser, and enable photo uploads.

Vercel's Python runtime deploys functions from `api/`; this project routes `/api/*` to that function. Vercel function files do not provide durable local SQLite storage, so product, order, settings, session, and admin data use Turso. Product photos use Blob storage. Uploaded images are public product content; customer order details stay in the database. The live site will not work until the environment variables are added and the database has been initialized with the setup file.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the alternative Docker/Waitress/Caddy deployment scaffold. Complete the deployment steps and replace placeholder business and policy information before taking real customer orders.

Add your legal business name, contact email/phone, return address, and final shipping/return/privacy/terms text before taking real orders. Policy pages currently contain editable starter language and an owner reminder.

Cash on Delivery is active. Online checkout is disabled. To offer it, choose a real payment provider, create a merchant account, configure server-side credentials outside the source code, and add verified payment and webhook handling before enabling the option. No payment is simulated by this site.

There is no SMS or email provider connected. My Orders works in the placing browser; a cross-device OTP order lookup would require an SMS provider. WhatsApp support appears only after you enter your real number in Admin → Settings.

## Product management

In Admin → Products, add the title, category, description, prices, internal supplier cost, stock state, variants, specifications, and product photos. Vercel uploads accept JPG, PNG, WebP, and GIF up to 3 MB each through Vercel Blob. Supplier cost and margin are only returned to the authenticated admin and never shown to shoppers.

