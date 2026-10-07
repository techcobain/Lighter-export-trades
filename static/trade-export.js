// Lighter exports have a per-request cap, so complete history is fetched in ranges.
const LighterTradeExport = (() => {
    const GENESIS_MS = Date.UTC(2025, 0, 17);
    const MAX_WINDOW_MS = 180 * 24 * 60 * 60 * 1000;
    const REQUEST_DELAY_MS = 3500;
    const MAX_RETRIES = 5;

    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

    const fundingKey = row => JSON.stringify([row.market, row.side, row.datetime_utc]);
    const rawFundingKey = row => JSON.stringify([row.Market, row.Side, row.Date]);

    function fetchTrades(options) {
        return fetchData(options, {
            type: 'trade', label: 'trade', plural: 'trades', raw: 'raw_trades',
            maxWindow: MAX_WINDOW_MS,
            key: row => String(row.trade_id), rawKey: row => String(row['Trade ID']),
        });
    }

    function fetchFundings(options) {
        return fetchData(options, {
            type: 'funding', label: 'funding', plural: 'fundings', raw: 'raw_fundings',
            maxWindow: 90 * 24 * 60 * 60 * 1000,
            sides: ['long', 'short'],
            key: fundingKey, rawKey: rawFundingKey,
        });
    }

    async function fetchData({ apiBase, authToken, accountIndex,
        fromTimestamp = null, toTimestamp = null, onProgress = () => {} }, config) {
        const start = Math.max(fromTimestamp ?? GENESIS_MS, GENESIS_MS);
        const end = Math.min(toTimestamp ?? Date.now(), Date.now());
        if (!Number.isFinite(start) || !Number.isFinite(end)) {
            throw new Error(`Invalid ${config.label} export date range`);
        }
        if (start > end) return { [config.plural]: [], [config.raw]: [] };

        const recordsByKey = new Map();
        const rawByKey = new Map();
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
                    // Lighter uses this specific error for a valid range with no records.
                    if (allowEmptyExport && response.status === 400) {
                        const data = await response.json();
                        if (data.code === 22504) return data;
                    }
                    // Never expose API messages that could include credentials or signed URLs.
                    throw new Error(`${config.label} export failed (HTTP ${response.status}). Please try again.`);
                }
                return response.json();
            }
        }

        async function fetchRange(rangeStart, rangeEnd, side) {
            if (requestCount > 0) await wait(REQUEST_DELAY_MS);
            requestCount++;
            const dates = [rangeStart, rangeEnd].map(ms => new Date(ms).toISOString().slice(0, 10));
            onProgress(`Account #${accountIndex}: Exporting ${dates[0]} to ${dates[1]} (${recordsByKey.size} ${config.plural} fetched)...`);
            const params = new URLSearchParams({
                account_index: accountIndex,
                type: config.type,
                aggregate: 'false',
                start_timestamp: rangeStart,
                end_timestamp: rangeEnd,
            });
            if (side) params.set('side', side);
            const exported = await requestJSON(`${apiBase}/api/v1/export?${params}`, {
                headers: { Authorization: authToken },
            }, true);
            if (exported.code === 22504) return;
            if (exported.code !== 200 || !exported.data_url) {
                throw new Error(`Lighter could not create the ${config.label} export. Please try again.`);
            }

            onProgress(`Account #${accountIndex}: Downloading and processing export...`);
            const processed = await requestJSON(`/api/process-${config.plural}-export`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ data_url: exported.data_url }),
            });
            if (!processed.success) throw new Error(`${config.label} export processing failed.`);
            if (processed.limit_reached) {
                // Overlap boundaries and deduplicate keys to preserve boundary records.
                const middle = Math.floor((rangeStart + rangeEnd) / 2);
                if (middle <= rangeStart || middle >= rangeEnd) {
                    throw new Error(`Too many ${config.plural} in a single millisecond to export completely.`);
                }
                await fetchRange(rangeStart, middle, side);
                await fetchRange(middle, rangeEnd, side);
                return;
            }
            if (!Array.isArray(processed[config.plural]) || !Array.isArray(processed[config.raw])
                || processed[config.plural].length !== processed[config.raw].length) {
                throw new Error(`Incomplete ${config.label} export response.`);
            }
            for (const record of processed[config.plural]) recordsByKey.set(config.key(record), record);
            for (const row of processed[config.raw]) rawByKey.set(config.rawKey(row), row);
        }

        let rangeEnd = end;
        while (true) {
            const rangeStart = Math.max(start, rangeEnd - config.maxWindow);
            // The live funding API returns incomplete payments for side=all. Request each side explicitly.
            for (const side of config.sides || [null]) await fetchRange(rangeStart, rangeEnd, side);
            if (rangeStart === start) break;
            rangeEnd = rangeStart;
        }

        const records = Array.from(recordsByKey.values()).sort((a, b) =>
            b.datetime_utc.localeCompare(a.datetime_utc) || config.key(b).localeCompare(config.key(a), undefined, { numeric: true }));
        const rawRecords = Array.from(rawByKey.values()).sort((a, b) =>
            b.Date.localeCompare(a.Date) || config.rawKey(b).localeCompare(config.rawKey(a), undefined, { numeric: true }));
        return { [config.plural]: records, [config.raw]: rawRecords };
    }

    return { fetchTrades, fetchFundings };
})();
