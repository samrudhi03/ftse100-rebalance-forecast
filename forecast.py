"""Forecast FTSE 100 additions and removals at a quarterly review.

The FTSE 100 holds the 100 largest London-listed companies by market cap and the
FTSE 250 holds the next 250. Together they form the FTSE 350, which is the
candidate pool here. At each quarterly review FTSE Russell applies a buffer:

    rank <=  90  and currently outside the FTSE 100  ->  promoted in
    rank >= 111  and currently inside the FTSE 100   ->  demoted out
    ranks 91-110                                     ->  buffer, left alone

Changes are made one for one so the index stays at exactly 100.

The script produces two things:

  1. A leakage-free backtest of the June 2026 review. Today's constituent lists
     already contain the June outcome, so membership is rewound to how it stood
     before that review and companies are sized on their 2 June closing prices.
     Neither the membership nor the prices know what the review decided.

  2. A live forward forecast for the September 2026 review, from today's
     membership and today's market caps. That review has not happened, so this
     one has no scorecard yet.

This script ranks on full market cap. FTSE ranks on free-float-adjusted
investable market cap, so boundary names may be misclassified. See the README
limitations section — that gap is the main source of error, not a detail.

Usage:  python forecast.py
"""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import requests
import yfinance as yf

FTSE_100_URL = "https://en.wikipedia.org/wiki/FTSE_100_Index"
FTSE_250_URL = "https://en.wikipedia.org/wiki/FTSE_250_Index"
HEADERS = {"User-Agent": "ftse100-rebalance-forecast (educational portfolio project)"}

PROMOTION_RANK = 90    # rank 90 or better and outside the index -> comes in
DEMOTION_RANK = 111    # rank 111 or worse and inside the index -> goes out

# FTSE measures size at close on the Tuesday before the first Friday of the
# review month. The first Friday of June 2026 was the 5th, so the cutoff is the
# 2nd. Everything in the backtest is measured on that date.
JUNE_CUTOFF = "2026-06-02"

OUTPUT_DIR = Path(__file__).parent / "output"
CSV_PATH = OUTPUT_DIR / "ftse100_rebalance_forecast.csv"
CHART_PATH = OUTPUT_DIR / "danger_zone.png"

# Confirmed outcome of the June 2026 review. Used to rewind membership and to
# score the backtest. The model is never tuned against it.
JUNE_2026_ADDED = ["Aberdeen Group", "Computacenter", "Investec"]
JUNE_2026_REMOVED = ["Berkeley Group Holdings", "Mondi", "Rightmove"]

# Colours: green = predicted add, red = predicted remove, grey = no change.
COLOUR_ADD = "#0ca30c"
COLOUR_REMOVE = "#d03b3b"
COLOUR_NEUTRAL = "#b8b7b0"
INK = "#0b0b0b"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
SURFACE = "#fcfcfb"


def fetch_constituents(url, index_name):
    """Scrape one constituent table off Wikipedia and tag it with its index."""
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    for table in pd.read_html(StringIO(response.text)):
        # The constituents table is the only large one with these two columns.
        if {"Company", "Ticker"} <= set(table.columns) and len(table) > 50:
            constituents = table[["Company", "Ticker"]].copy()
            constituents["current_index"] = index_name
            # Wikipedia's FTSE 250 table carries a trailing blank row.
            constituents = constituents.dropna(subset=["Company", "Ticker"])
            return constituents.reset_index(drop=True)

    raise RuntimeError(f"No constituents table found at {url}")


def build_candidate_pool():
    """Combine the FTSE 100 and FTSE 250 into the FTSE 350 candidate pool."""
    ftse_100 = fetch_constituents(FTSE_100_URL, "FTSE 100")
    ftse_250 = fetch_constituents(FTSE_250_URL, "FTSE 250")
    print(f"Scraped {len(ftse_100)} FTSE 100 and {len(ftse_250)} FTSE 250 constituents.")

    pool = pd.concat([ftse_100, ftse_250], ignore_index=True)

    # Wikipedia lists bare LSE codes. yfinance wants a .L suffix, and writes
    # multi-class codes with a hyphen rather than a dot (BT.A -> BT-A.L).
    pool["ticker"] = pool["Ticker"].str.strip().str.replace(".", "-", regex=False) + ".L"
    pool = pool.rename(columns={"Company": "company_name"})
    return pool[["ticker", "company_name", "current_index"]]


def fetch_one(ticker):
    """Pull the fields needed to size and sanity-check one company."""
    try:
        info = yf.Ticker(ticker).info
    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)}

    return {
        "ticker": ticker,
        "raw_market_cap": info.get("marketCap"),
        "currency": info.get("currency"),
        "price": info.get("regularMarketPrice"),
        "shares": info.get("sharesOutstanding"),
        "error": None,
    }


def fetch_market_caps(tickers):
    """Fetch market cap data for every ticker. Threaded purely for speed."""
    print(f"Fetching market caps for {len(tickers)} tickers...")
    with ThreadPoolExecutor(max_workers=12) as pool:
        records = list(pool.map(fetch_one, tickers))
    return pd.DataFrame(records)


def price_to_gbp(price, currency, rates):
    """Convert a quoted share price to pounds.

    THE DATA TRAP, handled deliberately. yfinance quotes London stocks in pence
    (currency code "GBp"), not pounds. Miss that and a company is sized 100x too
    large — and only some companies are, so the ranking still looks plausible
    while being wrong. This applies to historical closes exactly as it does to
    live prices, so both go through this function.
    """
    if price is None or pd.isna(price) or price <= 0:
        return None
    if currency == "GBp":
        return price / 100
    rate = rates.get(currency)
    return price * rate if rate else None


def fetch_fx_rates(date=None):
    """GBP per unit of foreign currency, live or as at a given date.

    Read off daily closes rather than fast_info, which intermittently fails right
    after a large batch of requests.
    """
    rates = {"GBP": 1.0}
    for currency in ("USD", "EUR"):
        pair = yf.Ticker(f"GBP{currency}=X")
        if date is None:
            history = pair.history(period="5d")
        else:
            history = pair.history(start=date, end=pd.Timestamp(date) + pd.Timedelta(days=1))
        if history.empty:
            raise RuntimeError(f"Could not fetch GBP{currency} rate for {date or 'today'}")
        rates[currency] = 1 / history["Close"].iloc[-1]  # the pair quotes foreign per GBP
    return rates


def normalise_market_caps(df, rates):
    """Convert every reported market cap to pounds.

    `Ticker.info["marketCap"]` is normally already in pounds even though the price
    is in pence, but that is undocumented yfinance behaviour that could change.
    Rather than trust it we check the scale: market cap should be within a small
    multiple of price x shares once pence are converted. If it is ~100x that, the
    value is still on the pence scale and we divide it down.

    The ratio is not exactly 1 even when the scale is right, because dual-listed
    companies (Rio Tinto, Investec) and multi-class companies have shares that
    `sharesOutstanding` does not count. That is why the trigger is a loose 10x
    rather than a tight tolerance: we are detecting a factor-of-100 error, not
    auditing the share count.
    """
    market_caps = []
    pence_corrections = []

    for row in df.itertuples():
        cap = row.raw_market_cap
        if not cap or cap <= 0:
            market_caps.append(None)
            continue

        if row.currency == "GBp" and row.price and row.shares:
            implied_pounds = row.price * row.shares / 100
            if cap / implied_pounds > 10:
                cap = cap / 100
                pence_corrections.append(row.ticker)

        # The cap is now in the major unit of the quote currency; convert to GBP.
        rate = 1.0 if row.currency == "GBp" else rates.get(row.currency)
        market_caps.append(cap * rate if rate else None)

    df = df.copy()
    df["market_cap"] = market_caps
    df["price_gbp"] = [price_to_gbp(r.price, r.currency, rates) for r in df.itertuples()]

    print(f"Pence-scale corrections applied: {len(pence_corrections)}")
    if pence_corrections:
        print(f"  {', '.join(pence_corrections)}")

    foreign = df[df["currency"].isin(["USD", "EUR"])]
    print(f"Non-GBP quoted lines converted to GBP: {len(foreign)}"
          f" ({', '.join(foreign['ticker'])})")
    return df


def report_failures(df, column, label):
    """Say out loud which tickers gave us nothing, rather than dropping them."""
    failed = df[df[column].isna()]
    print(f"{label}: {len(failed)} of {len(df)} tickers with no usable value")
    for row in failed.itertuples():
        print(f"  {row.ticker:10s} {row.company_name:35.35s}")
    if len(failed):
        print("  Excluded from the ranking, flagged 'no data' rather than dropped.")
    return failed


def apply_rule(df, membership_column, cap_column):
    """Rank on market cap and apply the promotion / demotion / buffer rule.

    Returns the frame with `rank` and `predicted_action` columns filled in.
    """
    df = df.copy()
    df["rank"] = df[cap_column].rank(ascending=False, method="first")
    df["predicted_action"] = "no change"
    df.loc[df[cap_column].isna(), "predicted_action"] = "no data"

    inside = df[membership_column] == "FTSE 100"
    outside = df[membership_column] == "FTSE 250"
    ranked = df["rank"].notna()

    # Step 1: the threshold rule. Both conditions check membership as well as
    # rank, so a company is only promoted if it is actually outside the index and
    # only demoted if it is actually inside it.
    qualifying_adds = ranked & outside & (df["rank"] <= PROMOTION_RANK)
    qualifying_removes = ranked & inside & (df["rank"] >= DEMOTION_RANK)
    df.loc[qualifying_adds, "predicted_action"] = "add"
    df.loc[qualifying_removes, "predicted_action"] = "remove"

    # Step 2: the one-for-one matching rule. The FTSE 100 must hold exactly 100
    # companies, so every promotion needs a matching demotion. The threshold rule
    # rarely produces equal numbers, because the buffer means a company can
    # qualify to come in while nobody has fallen far enough to drop out.
    #
    # Whichever side is short is topped up from the ranking edge. Note this can
    # demote a company sitting inside the buffer: it is not being demoted for
    # falling to 111, it is being demoted because it is the smallest member and
    # somebody has to make room.
    n_add = qualifying_adds.sum()
    n_remove = qualifying_removes.sum()
    balanced = []

    if n_add > n_remove:
        shortfall = n_add - n_remove
        eligible = df[inside & ranked & (df["predicted_action"] == "no change")]
        extra = eligible.nlargest(shortfall, "rank").index  # largest rank = smallest company
        df.loc[extra, "predicted_action"] = "remove"
        balanced = [(df.at[i, "company_name"], df.at[i, "rank"], "demoted") for i in extra]
    elif n_remove > n_add:
        shortfall = n_remove - n_add
        eligible = df[outside & ranked & (df["predicted_action"] == "no change")]
        extra = eligible.nsmallest(shortfall, "rank").index  # smallest rank = largest company
        df.loc[extra, "predicted_action"] = "add"
        balanced = [(df.at[i, "company_name"], df.at[i, "rank"], "promoted") for i in extra]

    print(f"Threshold rule: {n_add} qualifying addition(s), {n_remove} qualifying removal(s).")
    if balanced:
        print(f"One-for-one balancing: {len(balanced)} further change(s) to keep the index at 100.")
        for name, rank, how in balanced:
            print(f"  {name} (rank {int(rank)}) {how} to balance, not by the rank threshold.")
    else:
        print("One-for-one balancing: not needed, the two sides already match.")

    return df.sort_values("rank", na_position="last").reset_index(drop=True)


def print_changes(df, cap_column):
    """Print the predicted adds and removes."""
    for action in ("add", "remove"):
        flagged = df[df["predicted_action"] == action]
        print(f"\nPredicted {action}s: {len(flagged)}")
        for row in flagged.itertuples():
            cap = getattr(row, cap_column)
            print(f"  rank {int(row.rank):>3}  {row.company_name:35.35s} £{cap / 1e9:>7.2f}bn")


# ---------------------------------------------------------------------------
# Part 1: leakage-free backtest of the June 2026 review
# ---------------------------------------------------------------------------

def reconstruct_pre_june_membership(df):
    """Rebuild FTSE 100 membership as it stood *before* the June 2026 review.

    Today's constituent lists are the "after" picture: the June changes are
    already baked in. Aberdeen, Computacenter and Investec sit in the FTSE 100
    today precisely because June put them there. Ranking against that membership
    would be asking the model a question whose answer is already in its input.

    So we reverse the six changes to recover the "before" picture:
      - the three ADDED in June go back to the FTSE 250 (they were outside)
      - the three REMOVED in June go back to the FTSE 100 (they were inside)

    Both sides are flipped in the same column, so the pool stays consistent and
    still splits 100 / 250.
    """
    df = df.copy()
    df["pre_june_index"] = df["current_index"]
    df.loc[df["company_name"].isin(JUNE_2026_ADDED), "pre_june_index"] = "FTSE 250"
    df.loc[df["company_name"].isin(JUNE_2026_REMOVED), "pre_june_index"] = "FTSE 100"

    matched = df["company_name"].isin(JUNE_2026_ADDED + JUNE_2026_REMOVED).sum()
    if matched != 6:
        print(f"  Warning: matched {matched} of the 6 review names, expected 6.")
    print(f"  Rewound membership: {(df['pre_june_index'] == 'FTSE 100').sum()} in the "
          f"FTSE 100, {(df['pre_june_index'] == 'FTSE 250').sum()} in the FTSE 250.")
    return df


def fetch_june_prices(tickers):
    """Closing prices on the June cutoff date, in each ticker's quote currency."""
    print(f"  Fetching closing prices for {JUNE_CUTOFF}...")
    window_start = pd.Timestamp(JUNE_CUTOFF) - pd.Timedelta(days=4)
    window_end = pd.Timestamp(JUNE_CUTOFF) + pd.Timedelta(days=1)

    frame = yf.download(list(tickers), start=window_start, end=window_end,
                        auto_adjust=False, progress=False)["Close"]
    cutoff_rows = frame[frame.index.strftime("%Y-%m-%d") == JUNE_CUTOFF]
    if cutoff_rows.empty:
        raise RuntimeError(f"No market data returned for the {JUNE_CUTOFF} cutoff")

    return cutoff_rows.iloc[0]


def compute_june_market_caps(df, june_prices, june_rates):
    """Size every company as at the June cutoff.

    yfinance does not serve historical share counts, so shares are approximated:
    today's market cap divided by today's price gives implied shares outstanding,
    which we hold constant and multiply by the closing price on the cutoff date.
    A company that has issued or bought back stock since June is mis-sized by
    however much the count moved.

    Both prices go through `price_to_gbp`, so the pence-versus-pounds trap is
    handled on the historical closes exactly as it is on the live ones.
    """
    df = df.copy()
    df["june_price"] = df["ticker"].map(june_prices)

    june_caps = []
    for row in df.itertuples():
        june_price_gbp = price_to_gbp(row.june_price, row.currency, june_rates)
        if not june_price_gbp or not row.price_gbp or pd.isna(row.market_cap):
            june_caps.append(None)
            continue

        implied_shares = row.market_cap / row.price_gbp
        june_caps.append(implied_shares * june_price_gbp)

    df["june_market_cap"] = june_caps
    return df


def backtest_june_2026(df):
    """Replay the June 2026 review with no knowledge of its outcome."""
    print("\n" + "=" * 72)
    print(f"PART 1 - BACKTEST OF THE JUNE 2026 REVIEW (cutoff {JUNE_CUTOFF})")
    print("=" * 72)

    pre_june = reconstruct_pre_june_membership(df)
    june_rates = fetch_fx_rates(date=JUNE_CUTOFF)
    print(f"  FX on {JUNE_CUTOFF}: 1 USD = {june_rates['USD']:.4f} GBP, "
          f"1 EUR = {june_rates['EUR']:.4f} GBP")

    june_prices = fetch_june_prices(pre_june["ticker"])
    pre_june = compute_june_market_caps(pre_june, june_prices, june_rates)
    report_failures(pre_june, "june_market_cap", f"  Priced at {JUNE_CUTOFF}")

    replay = apply_rule(pre_june, membership_column="pre_june_index",
                        cap_column="june_market_cap")
    print_changes(replay, "june_market_cap")

    predicted_adds = set(replay.loc[replay["predicted_action"] == "add", "company_name"])
    predicted_removes = set(replay.loc[replay["predicted_action"] == "remove", "company_name"])

    print("\nAgainst the confirmed June 2026 outcome:")
    hits = 0
    for name in JUNE_2026_ADDED:
        hit = name in predicted_adds
        hits += hit
        print(f"  {'HIT ' if hit else 'MISS'}  add     {name}")
    for name in JUNE_2026_REMOVED:
        hit = name in predicted_removes
        hits += hit
        print(f"  {'HIT ' if hit else 'MISS'}  remove  {name}")

    false_positives = ((predicted_adds - set(JUNE_2026_ADDED))
                       | (predicted_removes - set(JUNE_2026_REMOVED)))
    print(f"\nHIT COUNT: {hits} of 6 confirmed changes correctly identified.")
    print(f"False positives (predicted but did not happen): {len(false_positives)}"
          f"{' - ' + ', '.join(sorted(false_positives)) if false_positives else ''}")

    # Where the misses sat, so the reader can see how close they were.
    missed = [n for n in JUNE_2026_ADDED if n not in predicted_adds]
    missed += [n for n in JUNE_2026_REMOVED if n not in predicted_removes]
    if missed:
        print("\nWhere the misses ranked on 2 June data:")
        for name in missed:
            row = replay[replay["company_name"] == name]
            if not row.empty:
                r = row.iloc[0]
                print(f"  {name:30.30s} rank {int(r['rank']):>3}  "
                      f"(was {r['pre_june_index']} before the review)")

    return hits


# ---------------------------------------------------------------------------
# Part 2: live forward forecast
# ---------------------------------------------------------------------------

def live_forecast(df):
    """Forecast the next review from today's membership and today's market caps."""
    print("\n" + "=" * 72)
    print("PART 2 - LIVE FORECAST: SEPTEMBER 2026 REVIEW")
    print("=" * 72)
    print("This review has not happened yet, so there is no outcome to score against.")

    forecast = apply_rule(df, membership_column="current_index", cap_column="market_cap")
    print_changes(forecast, "market_cap")
    return forecast


def make_chart(df, path):
    """Plot the companies ranked 85 to 115 — the danger zone around the boundary."""
    zone = df[df["rank"].between(85, 115)].sort_values("rank", ascending=False)

    colours = {"add": COLOUR_ADD, "remove": COLOUR_REMOVE}
    bar_colours = [colours.get(a, COLOUR_NEUTRAL) for a in zone["predicted_action"]]

    # Tag each row with the index it sits in today. This is a separate channel
    # from the bar colour on purpose: colour says what we predict will happen,
    # the label says where the company starts. Rank alone does not tell you.
    labels = [f"{int(r.rank):>3}  {r.company_name} ({r.current_index})"
              for r in zone.itertuples()]

    fig, ax = plt.subplots(figsize=(11, 10))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    positions = range(len(zone))
    ax.barh(positions, zone["market_cap"] / 1e9, color=bar_colours, height=0.72)

    # Shade the buffer: ranks 91 to 110 are left unchanged by the threshold rule.
    buffer_rows = [i for i, r in enumerate(zone["rank"]) if PROMOTION_RANK < r < DEMOTION_RANK]
    if buffer_rows:
        ax.axhspan(min(buffer_rows) - 0.5, max(buffer_rows) + 0.5,
                   color=INK_MUTED, alpha=0.08, zorder=0)
        ax.text(0.99, (min(buffer_rows) + max(buffer_rows)) / 2,
                "buffer zone\nranks 91-110", transform=ax.get_yaxis_transform(),
                ha="right", va="center", fontsize=9, color=INK_MUTED, linespacing=1.5)

    # Label only the at-risk names, so the eye goes straight to them.
    for i, (cap, action) in enumerate(zip(zone["market_cap"], zone["predicted_action"])):
        if action in colours:
            ax.text(cap / 1e9 + 0.25, i, action.upper(), va="center", fontsize=8.5,
                    fontweight="bold", color=colours[action])

    ax.set_yticks(list(positions))
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_ylim(-0.7, len(zone) - 0.3)
    ax.set_xlabel("Full market cap (£bn)", fontsize=10, color="#52514e")

    ax.tick_params(colors=INK_MUTED, length=0)
    for label in ax.get_yticklabels():
        label.set_color(INK)
    ax.xaxis.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")

    handles = [plt.Rectangle((0, 0), 1, 1, color=c)
               for c in (COLOUR_ADD, COLOUR_REMOVE, COLOUR_NEUTRAL)]
    ax.legend(handles, ["Predicted addition", "Predicted removal", "No change"],
              loc="lower right", frameon=False, fontsize=9.5)

    # Lay the axes out first, reserving the top 10% of the figure, then place the
    # title and subtitle into that reserved band in figure coordinates. Doing it
    # in this order means neither can be shifted by the layout pass, which is what
    # caused them to collide when the title belonged to the axes.
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    left = ax.get_position().x0

    fig.text(left, 0.985, "FTSE 100 danger zone: September 2026 review forecast",
             fontsize=13, fontweight="bold", color=INK, va="top")
    fig.text(left, 0.951,
             "Companies ranked 85-115 by market cap. Promotion at rank 90 or better, "
             "demotion at 111 or worse.\nLabel shows the index each company is in today.",
             fontsize=9.5, color=INK_MUTED, va="top", linespacing=1.4)

    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"Saved chart to {path}")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    pool = build_candidate_pool()
    caps = fetch_market_caps(pool["ticker"])
    df = pool.merge(caps, on="ticker", how="left")

    rates = fetch_fx_rates()
    print(f"FX today: 1 USD = {rates['USD']:.4f} GBP, 1 EUR = {rates['EUR']:.4f} GBP")
    df = normalise_market_caps(df, rates)
    report_failures(df, "market_cap", "Live market caps")

    backtest_june_2026(df)
    forecast = live_forecast(df)

    columns = ["ticker", "company_name", "current_index", "market_cap", "rank",
               "predicted_action"]
    forecast[columns].to_csv(CSV_PATH, index=False)
    print(f"\nSaved {len(forecast)} rows to {CSV_PATH}")

    make_chart(forecast, CHART_PATH)


if __name__ == "__main__":
    main()
