# Deploy Guide — no command line required

Everything below is clicking through websites. You don't need to install
anything or use a terminal. Should take about 20-30 minutes the first time.

## Step 1: Put the code on GitHub

GitHub is just a place to store the code so Render (the hosting service)
can pick it up. It's free.

1. Go to https://github.com and create a free account, if you don't
   already have one.
2. Once logged in, click the **+** icon (top right) → **New repository**.
3. Name it `patchalert`. Leave it Public or Private (your choice — Private
   is fine and free). Don't tick any of the "initialize with README"
   options. Click **Create repository**.
4. On the next page, look for a link that says **uploading an existing
   file**. Click it.
5. Unzip the `planning-alerts.zip` file I sent you on your computer first
   (double-click it, or right-click → Extract). Then drag the whole
   contents of the unzipped `planning-alerts` folder into the GitHub
   upload box (drag the files/folders themselves, not the outer folder).
6. Scroll down, click **Commit changes**.

Your code is now on GitHub.

## Step 2: Create a Render account and deploy

1. Go to https://render.com and sign up — the easiest way is "Sign up
   with GitHub," which also connects the two automatically.
2. Once logged in, click **New +** → **Blueprint**.
3. Pick the `patchalert` repository you just created. Render will read
   the `render.yaml` file in it automatically and show you what it's
   about to create (a web service).
4. Click **Apply** / **Create**. Render will build and deploy it — takes
   a few minutes the first time. You'll get a URL like
   `https://patchalert.onrender.com` when it's done.
5. Visit that URL — you should see the homepage, live on the internet.

## Step 3: Add the environment variables

Still in Render, open your new service → **Environment** tab → add these
(click "Add Environment Variable" for each):

| Key | Value |
|---|---|
| `DIGEST_TRIGGER_SECRET` | Make up any random string, e.g. `pk_7f3a9d2e1c` |

That's the only one required to get the site fully working end to end
(signups, previews). The others below are optional and can be added
later, whenever you're ready for that piece:

| Key | When to add it |
|---|---|
| `RESEND_API_KEY`, `RESEND_FROM_ADDRESS` | Once you've signed up for Resend and want real emails sent |
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID` | Once you've set up Stripe and want to charge for it |

After adding a variable, Render will automatically redeploy the service.

## Step 4: Verify the real planning data works

This is the one thing that genuinely couldn't be tested before now, since
my build environment has no general internet access. Once your Render
service is live:

1. Visit `https://YOUR-APP-URL/signup` and sign up with a real postcode
   you're familiar with (so you can sanity-check the results).
2. You'll land on the preview page. If it shows real, sensible-looking
   planning applications for that area, the live PlanIt API integration
   works. If it errors out or looks wrong, send me the error message (or
   just tell me it broke) and I'll fix `planit_client.py`.

## Step 5: Turn on the daily scheduled digest

1. In your GitHub repository, go to **Settings** → **Secrets and
   variables** → **Actions** → **New repository secret**.
2. Add `APP_URL` with your Render URL (e.g. `https://patchalert.onrender.com`,
   no trailing slash).
3. Add `DIGEST_TRIGGER_SECRET` with the exact same value you set in Render.
4. That's it — `.github/workflows/daily-digest.yml` is already in the
   code and will run automatically once a day. You can also trigger it
   manually any time from the repo's **Actions** tab, to test it works.

## You're live

At this point, the site is real, on the internet, and running the daily
job automatically. The only thing between here and having customers is
posting in trade groups (see `MARKETING_COPY.md`) — tell me once you've
done that and I'll help you monitor signups and iterate.
