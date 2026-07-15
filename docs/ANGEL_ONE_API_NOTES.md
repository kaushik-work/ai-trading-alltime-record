# Angel One SmartAPI Notes

Compiled from:
- https://smartapi.angelbroking.com/docs
- https://github.com/angel-one/smartapi-python
- https://www.angelone.in/knowledge-center/smartapi

## Base URLs

- Production REST: `https://apiconnect.angelone.in`
- Login page: `https://smartapi.angelone.in/publisher-login`
- Scrip master: `https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json`

## Authentication

- `generateSession(client_code, password, totp)` returns JWT + refresh + feed tokens.
- Tokens expire daily at midnight; bot must re-login every day.
- TOTP is generated from `ANGEL_TOTP_TOKEN` (base32 secret).
- Order placement APIs now require a static IP whitelisted with Angel One (SEBI circular).
- Other APIs (market data, order book, portfolio) do not require static IP.

## Key REST Routes

| SDK method | Endpoint |
|---|---|
| `generateSession` | POST `/rest/auth/angelbroking/user/v1/loginByPassword` |
| `generateToken` | POST `/rest/auth/angelbroking/jwt/v1/generateTokens` |
| `getProfile` | GET `/rest/secure/angelbroking/user/v1/getProfile` |
| `logout` | POST `/rest/secure/angelbroking/user/v1/logout` |
| `placeOrder` | POST `/rest/secure/angelbroking/order/v1/placeOrder` |
| `modifyOrder` | POST `/rest/secure/angelbroking/order/v1/modifyOrder` |
| `cancelOrder` | POST `/rest/secure/angelbroking/order/v1/cancelOrder` |
| `getOrderBook` | GET `/rest/secure/angelbroking/order/v1/getOrderBook` |
| `getTradeBook` | GET `/rest/secure/angelbroking/order/v1/getTradeBook` |
| `ltpData` | POST `/rest/secure/angelbroking/order/v1/getLtpData` |
| `getPosition` | GET `/rest/secure/angelbroking/order/v1/getPosition` |
| `getHolding` | GET `/rest/secure/angelbroking/portfolio/v1/getHolding` |
| `getRMS` | GET `/rest/secure/angelbroking/user/v1/getRMS` |
| `getCandleData` | POST `/rest/secure/angelbroking/historical/v1/getCandleData` |
| `getMarketData` | POST `/rest/secure/angelbroking/market/v1/quote` |
| `searchScrip` | POST `/rest/secure/angelbroking/order/v1/searchScrip` |
| `individualOrderDetails` | GET `/rest/secure/angelbroking/order/v1/details/{uniqueorderid}` |
| `getMarginApi` | POST `/rest/secure/angelbroking/margin/v1/batch` |
| `estimateCharges` | POST `/rest/secure/angelbroking/brokerage/v1/estimateCharges` |

## Order Placement (`placeOrder`)

Required fields:
- `variety`: NORMAL / STOPLOSS / AMO / ROBO / COVER (for options use NORMAL)
- `tradingsymbol`: e.g. `NIFTY24JUL2624500CE`
- `symboltoken`: from scrip master
- `transactiontype`: BUY / SELL
- `exchange`: NFO / BFO
- `ordertype`: MARKET / LIMIT / SL / SL-M
- `producttype`: CARRYFORWARD / INTRADAY / DELIVERY / MARGIN / BO / CO
  - For options, use `CARRYFORWARD` (NRML) or `INTRADAY` (MIS).
- `duration`: DAY / IOC
- `quantity`: in units (lot size multiple)
- `price`: limit price; "0" for MARKET
- `squareoff`, `stoploss`, `triggerprice`: "0" for simple orders

## Index Spot Tokens (stable)

| Index | Token | Trading Symbol | Exchange |
|---|---|---|---|
| NIFTY 50 | 99926000 | Nifty 50 | NSE |
| BANKNIFTY | 99926009 | Nifty Bank | NSE |
| FINNIFTY | 99926037 | Nifty Fin Service | NSE |
| SENSEX | 1 | SENSEX | BSE |

## Market Data (`getMarketData`)

- `mode`: LTP / OHLC / FULL / 15MIN
- `exchangeTokens`: `{"NFO": ["token1", ...], "BFO": [...]}`
- Response `fetched` array contains fields like `symbolToken`, `ltp`, `bidPrice`, `askPrice`, `open`, `high`, `low`, `close`, `tradeVolume`, `opnInterest` depending on mode.

## Option Symbol Format

Angel One trading symbol format for index options:
```
<INDEX><DD><MMM><YY><STRIKE><CE/PE>
```
Examples:
- `NIFTY24JUL2624500CE`
- `BANKNIFTY24JUL2652000PE`
- `SENSEX24JUL2681000CE`

The scrip master JSON is the authoritative source for `symboltoken` and `tradingsymbol`.

## Important Constraints

- Order rate limit: 9 orders/second.
- Static IP mandatory for place/modify/cancel order and GTT APIs.
- Session expires at midnight daily.
- Do not confuse lot size with quantity: `quantity = lots × lot_size`.

## Margin Calculator API (`getMarginApi`)

- **Endpoint**: `POST /rest/secure/angelbroking/margin/v1/batch`
- **Rate limit**: 10 requests/second
- **Max positions per request**: 50
- **Payload**:
```json
{
  "positions": [
    {
      "exchange": "NFO",
      "qty": 75,
      "price": 0,
      "productType": "CARRYFORWARD",
      "orderType": "MARKET",
      "token": "...",
      "tradeType": "BUY"
    }
  ]
}
```
- **Response**:
```json
{
  "status": true,
  "message": "SUCCESS",
  "errorcode": "",
  "data": {
    "totalMarginRequired": 29612.35,
    "marginComponents": {
      "netPremium": 5060.0,
      "spanMargin": 0.0,
      "marginBenefit": 79876.5,
      "deliveryMargin": 0.0,
      "nonNFOMargin": 0.0,
      "totOptionsPremium": 10100.0
    }
  }
}
```

Use this API before every order for exact SPAN + exposure margin. Do not rely on fixed per-lot estimates in production.

## Options Capital Reality

- **Option buying**: pay full premium; no leverage.
- **Option selling / short leg**: requires SPAN + exposure margin.
- **Synthetic forward (same strike, same expiry)**: behaves like a futures position; SPAN may recognize the long/short hedge and reduce margin compared to a naked short option.
- Margin varies continuously with strike, expiry, spot, and IV. A fixed budget table is only a rough planning guide; the live broker calculation is authoritative.
