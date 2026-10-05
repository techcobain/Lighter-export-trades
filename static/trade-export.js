// Lighter exports have a per-request cap, so complete history is fetched in ranges.
const LighterTradeExport = (() => {
    const GENESIS_MS = Date.UTC(2025, 0, 17);
    const MAX_WINDOW_MS = 180 * 24 * 60 * 60 * 1000;
    const REQUEST_DELAY_MS = 3500;
    const MAX_RETRIES = 5;

    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

    async function fetchTrades({ apiBase, authToken, accountIndex,
        fromTimestamp = null, toTimestamp = null, onProgress = () => {} }) {
        const start = Math.max(fromTimestamp ?? GENESIS_MS, GENESIS_MS);
        const end = Math.min(toTimestamp ?? Date.now(), Date.now());
        if (!Number.isFinite(start) || !Number.isFinite(end)) {
            throw new Error('Invalid trade export date range');
        }
        if (start > end) return { trades: [], raw_trades: [] };

        const tradesById = new Map();
        const rawById = new Map();
        let requestCount = 0;

        async function requestJSON(url, options, allowEmptyExport = false) {
            for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
                const response = await fetch(url, options);
                if ((response.status === 429 || response.status === 405) && attempt < MAX_RETRIES) {
                    const retryAfter = Number(response.headers.get('Retry-After'));
                    const delay = retryAfter > 0 ? retryAfter * 1000 : 15000;
                    onProgress(`Account #${accountIndex}: Rate limited, waiting ${Math.ceil(delay / 1000)}s...`);
                    await wait(delay);
                    continue;
                }
                if (!response.ok) {
                    // Lighter uses this specific error for a valid range with no fills.
                    if (allowEmptyExport && response.status === 400) {
                        const data = await response.json();
                        if (data.code === 22504) return data;
                    }
                    // Never expose API messages that could include credentials or signed URLs.
                    throw new Error(`Trade export failed (HTTP ${response.status}). Please try again.`);
                }
                return response.json();
            }
        }

        async function fetchRange(rangeStart, rangeEnd) {
            if (requestCount > 0) await wait(REQUEST_DELAY_MS);
            requestCount++;
            const dates = [rangeStart, rangeEnd].map(ms => new Date(ms).toISOString().slice(0, 10));
            onProgress(`Account #${accountIndex}: Exporting ${dates[0]} to ${dates[1]} (${tradesById.size} trades fetched)...`);
            const params = new URLSearchParams({
                account_index: accountIndex,
                type: 'trade',
                aggregate: 'false',
                start_timestamp: rangeStart,
                end_timestamp: rangeEnd,
            });
            const exported = await requestJSON(`${apiBase}/api/v1/export?${params}`, {
                headers: { Authorization: authToken },
            }, true);
            if (exported.code === 22504) return;
            if (exported.code !== 200 || !exported.data_url) {
                throw new Error('Lighter could not create the trade export. Please try again.');
            }

            onProgress(`Account #${accountIndex}: Downloading and processing export...`);
            const processed = await requestJSON('/api/process-trades-export', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ data_url: exported.data_url }),
            });
            if (!processed.success) throw new Error('Trade export processing failed.');
            if (processed.limit_reached) {
                // Overlap boundaries and deduplicate IDs to avoid dropping boundary fills.
                const middle = Math.floor((rangeStart + rangeEnd) / 2);
                if (middle <= rangeStart || middle >= rangeEnd) {
                    throw new Error('Too many trades in a single millisecond to export completely.');
                }
                await fetchRange(rangeStart, middle);
                await fetchRange(middle, rangeEnd);
                return;
            }
            if (!Array.isArray(processed.trades) || !Array.isArray(processed.raw_trades)
                || processed.trades.length !== processed.raw_trades.length) {
                throw new Error('Incomplete trade export response.');
            }
            for (const trade of processed.trades) tradesById.set(String(trade.trade_id), trade);
            for (const row of processed.raw_trades) rawById.set(String(row['Trade ID']), row);
        }

        let rangeEnd = end;
        while (true) {
            const rangeStart = Math.max(start, rangeEnd - MAX_WINDOW_MS);
            await fetchRange(rangeStart, rangeEnd);
            if (rangeStart === start) break;
            rangeEnd = rangeStart;
        }

        const trades = Array.from(tradesById.values()).sort((a, b) =>
            b.datetime_utc.localeCompare(a.datetime_utc) || b.trade_id - a.trade_id);
        const rawTrades = Array.from(rawById.values()).sort((a, b) =>
            b.Date.localeCompare(a.Date) || Number(b['Trade ID']) - Number(a['Trade ID']));
        return { trades, raw_trades: rawTrades };
    }

    return { fetchTrades };
})();
