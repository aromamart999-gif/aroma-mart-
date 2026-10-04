# Turn on the AMORA MART admin on Vercel

The currently published static copy cannot save orders or sign in to Admin. Deploy the complete project folder and connect a persistent database before taking customer orders.

## 1. Deploy the complete project

In Vercel → Project → Settings → General, set **Root Directory** to the folder containing `vercel.json`, `server.py`, `index.html`, `assets`, and `api`. Do not use the `public` subfolder as the project root. Clear any custom **Output Directory** setting so Vercel deploys the root files and Python function together. Push or upload the updated project files and redeploy.

The new `vercel.json` sends the shop's API to its Python function and sends shop pages such as `/admin` to the storefront app.

## 2. Connect permanent database storage

Create a database in [Turso](https://turso.tech/), then in Vercel → Project → Settings → Environment Variables, add these for **Production**:

- `TURSO_DATABASE_URL` — the database URL shown by Turso.
- `TURSO_AUTH_TOKEN` — the private database token shown by Turso. Never put it in a web page or public repository.
- `AMORA_COOKIE_SECURE` — set to `1`.
- `AMORA_PUBLIC_URL` — set to `https://aroma-mart.vercel.app` (or your actual site address).
- `AMORA_ADMIN_SETUP_TOKEN` — a private, random one-time code with at least 32 characters. A password manager's random password generator works well. Keep it out of the source files.

Save the values. If you change environment variables after a deployment, redeploy the site.

## 3. Create your private admin login

Redeploy after saving the environment variables. Then open `https://aroma-mart.vercel.app/admin/setup`. The setup page creates the database tables, starter listings, shoe rack, and your first admin account. Enter the exact one-time setup code you stored in Vercel, then choose an admin username and a password with at least 12 characters. The password is saved only as a salted hash. This does not upload the local SQLite file or any old customer orders.

Once setup succeeds, remove `AMORA_ADMIN_SETUP_TOKEN` from Vercel's environment settings and redeploy. Then open `https://aroma-mart.vercel.app/admin` and sign in with your new username and password. The setup endpoint refuses new accounts after the first admin has been created.

## 4. Enable permanent product photo uploads

In Vercel → Project → Storage, create a **public Blob store** and connect it to this project. Vercel adds `BLOB_READ_WRITE_TOKEN` to the project. Redeploy after connecting it. Then the product editor can save JPG, PNG, WebP, and GIF photos. Uploaded product photos are public; customer addresses and phone numbers remain private in the database.

Until Blob storage is connected, use public HTTPS image URLs in the product editor. Do not accept orders until the admin sign-in and database are ready.
