import yfinance as yf


def get_stock(ticker: str) -> str:
    t = yf.Ticker(ticker.upper())
    i = t.fast_info

    price = i.last_price
    prev = i.previous_close
    high = i.day_high
    low = i.day_low

    if price is None:
        return f"No data for {ticker.upper()}. Check the ticker symbol."

    change = price - prev
    pct = (change / prev * 100) if prev else 0
    sign = "+" if change >= 0 else ""

    return (
        f"{ticker.upper()}: ${price:.2f} ({sign}{change:.2f} {sign}{pct:.1f}%). "
        f"H ${high:.2f} L ${low:.2f}"
    )
