# Card Desk

Inventory, buying and selling for a trading-card stand: Pokémon (English and Japanese), Yu-Gi-Oh!, One Piece, Gundam and Riftbound.

- **App:** `site/index.html`, one page with no build step. It signs in with Google (Firebase Auth) and saves to Cloud Firestore, with an offline cache so it works at shows without signal.
- **Prices:** `sync/build_catalog.py` downloads the free nightly TCGplayer data from [TCGCSV](https://tcgcsv.com) once a day and writes `site/catalog/` (searchable card list + price lookup). It never runs in the browser.
- **Daily job:** `.github/workflows/deploy.yml` tries every hour from 6 AM to about noon New York time and builds as soon as TCGCSV has new prices (GitHub often starts scheduled runs late). It also runs on every push, and can be run by hand from the Actions tab. If any game fails to download, nothing is deployed and the previous day's prices stay live. A comment on the "Price update notifications" issue reports each run.
- **Price history:** `sync/build_history.py` keeps TCGplayer near-mint prices from June 4 2026 (weekly, daily for the last two weeks; cards worth $1+) in `site/catalog/history/`, from TCGCSV's daily archives. The app combines it with your stock, purchases and sales for the *Stock value over time* chart on Overview and Money. Graded and hand-priced cards use the values you enter (each change is saved on the card as `vlog`).
- **Data privacy:** this repository holds only the app and public card prices. Inventory, purchases, sales and expenses live in Firestore, readable only by the owner's Google account (enforced by Firestore security rules set in the Firebase console).

`site/vendor/firebase.js` is the Firebase web SDK 13.2.0 (app, auth, firestore) bundled with esbuild, so the app works offline without a CDN.
