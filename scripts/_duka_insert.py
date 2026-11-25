import duckdb, pandas as pd
df = pd.read_csv('data/dukascopy/xauusd-h1-bid-2022-01-03-2024-01-14.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
con = duckdb.connect('data/nexus_ext.duckdb')
con.execute("""CREATE TABLE IF NOT EXISTS ohlcv_ext (symbol VARCHAR, source VARCHAR, timeframe VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, timestamp TIMESTAMP WITH TIME ZONE)""")
con.executemany("INSERT INTO ohlcv_ext VALUES ('XAUUSD','DUKASCOPY','H1',?,?,?,?,?,?)",
    [(r.open, r.high, r.low, r.close, 0.0, r.timestamp) for r in df.itertuples()])
print("ext rows:", con.execute("select count(*) from ohlcv_ext").fetchone()[0])
con.close()
