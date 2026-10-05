const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = readFileSync(resolve(__dirname, '../static/trade-export.js'), 'utf8');
const genesis = Date.UTC(2025, 0, 17);
const windowMs = 180 * 86400000;

function createExporter(handler) {
    const calls = [];
    const sleeps = [];
    const context = vm.createContext({
        URLSearchParams, Date, console,
        setTimeout: (callback, delay) => { sleeps.push(delay); callback(); },
        fetch: async (url, options) => {
            calls.push({ url, options });
            return handler(url, options);
        },
    });
    vm.runInContext(source, context);
    return {
        run: options => vm.runInContext('LighterTradeExport.fetchTrades', context)({
            apiBase: 'https://mainnet.zklighter.elliot.ai',
            authToken: 'ro:test-only', accountIndex: 42, ...options,
        }),
        calls, sleeps,
    };
}

function response(data, status = 200, retryAfter = null) {
    return { ok: status === 200, status, headers: { get: () => retryAfter }, json: async () => data };
}

function processed(ids) {
    return {
        success: true, limit_reached: false,
        trades: ids.map(id => ({ trade_id: id, datetime_utc: '2026-09-01 12:00:00 UTC' })),
        raw_trades: ids.map(id => ({ 'Trade ID': String(id), Date: '2026-09-01 12:00:00' })),
    };
}

test('uses export with millisecond dates, preserves >3K fills, keeps auth out of server requests', async () => {
    const exporter = createExporter((url, options) => {
        if (url.startsWith('https://')) {
            const parsed = new URL(url);
            assert.equal(parsed.pathname, '/api/v1/export');
            assert.equal(parsed.searchParams.get('type'), 'trade');
            assert.equal(parsed.searchParams.get('aggregate'), 'false');
            assert.equal(parsed.searchParams.get('account_index'), '42');
            assert.equal(parsed.searchParams.get('start_timestamp'), String(genesis));
            assert.equal(parsed.searchParams.get('end_timestamp'), String(genesis + 1000));
            assert.equal(options.headers.Authorization, 'ro:test-only');
            assert.ok(!url.includes('ro:'));
            return response({ code: 200, data_url: 'https://storage.test/file' });
        }
        assert.equal(url, '/api/process-trades-export');
        assert.deepEqual(JSON.parse(options.body), { data_url: 'https://storage.test/file' });
        assert.ok(!JSON.stringify(options).includes('ro:test-only'));
        return response(processed(Array.from({ length: 3501 }, (_, i) => i)));
    });
    const result = await exporter.run({ fromTimestamp: genesis, toTimestamp: genesis + 1000 });
    assert.equal(result.trades.length, 3501);
    assert.equal(result.raw_trades.length, 3501);
    assert.equal(result.trades[0].trade_id, 3500);
});

test('complete history spans multiple ranges and deduplicates boundary trades', async () => {
    let exports = 0;
    const ranges = [];
    const exporter = createExporter(url => {
        if (url.startsWith('https://')) {
            const params = new URL(url).searchParams;
            ranges.push([Number(params.get('start_timestamp')), Number(params.get('end_timestamp'))]);
            exports++;
            return response({ code: 200, data_url: 'https://storage.test/file' });
        }
        return response(processed([exports, 999]));
    });
    const end = genesis + windowMs * 2 + 1000;
    const result = await exporter.run({ toTimestamp: end });
    assert.deepEqual(ranges, [[end - windowMs, end], [end - 2 * windowMs, end - windowMs], [genesis, end - 2 * windowMs]]);
    assert.equal(result.trades.length, 4);
    assert.equal(result.raw_trades.length, 4);
    assert.equal(exporter.sleeps.length, 2);
});

test('splits capped ranges without accepting truncated data', async () => {
    let count = 0;
    const exporter = createExporter(url => {
        if (url.startsWith('https://')) return response({ code: 200, data_url: 'https://storage.test/file' });
        count++;
        return response(count === 1 ? { success: true, limit_reached: true } : processed([count, 100]));
    });
    const result = await exporter.run({ fromTimestamp: genesis, toTimestamp: genesis + 1000 });
    assert.equal(count, 3);
    assert.equal(result.trades.length, 3);
    const ranges = exporter.calls.filter(call => call.url.startsWith('https://')).map(call => {
        const params = new URL(call.url).searchParams;
        return [Number(params.get('start_timestamp')), Number(params.get('end_timestamp'))];
    });
    assert.deepEqual(ranges, [[genesis, genesis + 1000], [genesis, genesis + 500], [genesis + 500, genesis + 1000]]);
});

test('honors Retry-After and retries 429 and Lighter 405 rate limits', async () => {
    let count = 0;
    const exporter = createExporter(url => {
        if (!url.startsWith('https://')) return response(processed([]));
        count++;
        return count < 3 ? response({}, count === 1 ? 429 : 405, '2')
            : response({ code: 200, data_url: 'https://storage.test/file' });
    });
    await exporter.run({ fromTimestamp: genesis, toTimestamp: genesis + 1000 });
    assert.deepEqual(exporter.sleeps, [2000, 2000]);
});

test('rate-limit retries are bounded', async () => {
    const exporter = createExporter(() => response({}, 429));
    await assert.rejects(exporter.run({ fromTimestamp: genesis, toTimestamp: genesis + 1000 }), /HTTP 429/);
    assert.equal(exporter.calls.length, 6);
});

test('rejects failed later exports rather than returning partial success', async () => {
    let count = 0;
    const exporter = createExporter(url => {
        if (!url.startsWith('https://')) return response(processed([1]));
        count++;
        return count === 1 ? response({ code: 200, data_url: 'https://storage.test/file' }) : response({}, 500);
    });
    await assert.rejects(exporter.run({ fromTimestamp: genesis, toTimestamp: genesis + windowMs + 1 }), /HTTP 500/);
});

test('rejects API-level errors and incomplete processing responses', async () => {
    for (const data of [{ code: 400 }, { code: 200 }]) {
        const exporter = createExporter(() => response(data));
        await assert.rejects(exporter.run({ fromTimestamp: genesis, toTimestamp: genesis + 1000 }), /could not create/);
    }
    const exporter = createExporter(url => response(url.startsWith('https://')
        ? { code: 200, data_url: 'https://storage.test/file' }
        : { success: true, trades: [{}], raw_trades: [] }));
    await assert.rejects(exporter.run({ fromTimestamp: genesis, toTimestamp: genesis + 1000 }), /Incomplete/);
});

test('empty and pre-genesis ranges return no trades', async () => {
    const exporter = createExporter(() => { throw new Error('should not fetch'); });
    const result = await exporter.run({ toTimestamp: genesis - 1 });
    assert.equal(result.trades.length, 0);
    assert.equal(exporter.calls.length, 0);
});

test('Lighter empty-export error skips empty ranges while other HTTP 400 errors fail', async () => {
    const empty = createExporter(() => response({ code: 22504 }, 400));
    const result = await empty.run({ fromTimestamp: genesis, toTimestamp: genesis + windowMs + 1 });
    assert.equal(result.trades.length, 0);
    assert.equal(empty.calls.length, 2);
    const badRequest = createExporter(() => response({ code: 12345 }, 400));
    await assert.rejects(badRequest.run({ fromTimestamp: genesis, toTimestamp: genesis + 1000 }), /HTTP 400/);
});

test('inline application scripts compile', () => {
    const html = readFileSync(resolve(__dirname, '../static/index.html'), 'utf8');
    for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
    assert.ok(html.includes('<script src="/static/trade-export.js"></script>'));
});

test('trade table formatting and CSV/JSON downloads retain their output structure', async () => {
    const html = readFileSync(resolve(__dirname, '../static/index.html'), 'utf8');
    const appSource = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
    const blobs = [];
    const downloads = [];
    const context = vm.createContext({
        console, Date, Blob,
        URL: { createObjectURL: blob => { blobs.push(blob); return 'blob:test'; }, revokeObjectURL: () => {} },
        setTimeout: callback => callback(),
        alert: message => { throw new Error(message); },
        document: {
            body: { appendChild: () => {} },
            createElement: () => ({ style: {}, click() { downloads.push(this.download); }, remove() {} }),
        },
    });
    vm.runInContext(appSource.slice(0, appSource.indexOf('// Initialize on page load')), context);
    vm.runInContext(`
        fetchedData = {42: {trades: [{trade_id: 7, market: 'LIT', market_type: 'Spot', side: 'Buy',
            datetime_utc: '2026-09-19 15:30:50 UTC', fee_usd: 0.0001, pnl_usd: null}]}};
        rawTradesData = {42: [{'Trade ID': '7', Market: 'LIT/USDC', Side: 'Buy'}]};
        selectedColumns = {market: true, side: true, fee_usd: true};
        exportCSV(42);
        exportJSON(42);
    `, context);
    assert.equal(await blobs[0].text(), 'Market,Side,Fee ($)\n"LIT","Buy",0.0001');
    const json = JSON.parse(await blobs[1].text());
    assert.deepEqual(Object.keys(json), ['exported_at', 'account_index', 'total_trades', 'trades']);
    assert.equal(json.account_index, 42);
    assert.equal(json.total_trades, 1);
    assert.deepEqual(json.trades, [{ 'Trade ID': '7', Market: 'LIT/USDC', Side: 'Buy' }]);
    assert.match(downloads[0], /^lighter_trades_account_42_.*\.csv$/);
    assert.match(downloads[1], /^lighter_trades_account_42_.*\.json$/);
    assert.match(vm.runInContext("formatCell({side: 'Buy'}, 'side')", context), /trade-side long/);
    assert.match(vm.runInContext("formatCell({tx_hash: ''}, 'tx_hash')", context), /N\/A/);
    assert.match(vm.runInContext("formatCell({pnl_usd: null}, 'pnl_usd')", context), /^-$/);
});
