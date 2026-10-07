const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.resolve(__dirname, '../static/account-statements.js'), 'utf8');
const now = Date.UTC(2026, 9, 7, 20);

function setup(handler = () => { throw new Error('Unexpected request'); }) {
    class FixedDate extends Date { static now() { return now; } }
    const calls = [];
    const context = vm.createContext({ Date: FixedDate, URLSearchParams,
        setTimeout: cb => cb(),
        fetch: async (url, options) => { calls.push({ url, options }); return handler(url, options); },
        LighterTradeExport: { fetchTrades: async () => ({ raw_trades: [] }), fetchFundings: async () => ({ raw_fundings: [] }) },
    });
    vm.runInContext(source, context);
    return { context, api: vm.runInContext('LighterAccountStatements', context), calls };
}

function response(data, status = 200) {
    return { ok: status === 200, status, json: async () => data, headers: { get: () => '1' } };
}

test('monthly cutoffs cover Jan–Sep, December rolls to next year, annual and custom use UTC', () => {
    const { api } = setup();
    const periods = api.cutoffs({ mode: 'monthly', year: 2026, startMonth: 1, endMonth: 9 }, now);
    assert.equal(periods.length, 9);
    assert.equal(periods[0].timestamp_ms, Date.UTC(2026, 1, 1));
    assert.equal(periods[8].timestamp_ms, Date.UTC(2026, 9, 1));
    const december = api.cutoffs({ mode: 'monthly', year: 2025, startMonth: 12, endMonth: 12 }, now);
    assert.equal(december[0].timestamp_ms, Date.UTC(2026, 0, 1));
    assert.equal(api.cutoffs({ mode: 'annual', year: 2025 }, now)[0].timestamp_ms, Date.UTC(2026, 0, 1));
    assert.equal(api.cutoffs({ mode: 'custom', customUTC: '2026-09-15T13:25' }, now)[0].timestamp_ms, Date.UTC(2026, 8, 15, 13, 25));
});

test('rejects future year/month ends, invalid calendar dates, reversed months and pre-genesis dates', () => {
    const { api } = setup();
    for (const options of [{ mode: 'annual', year: 2026 }, { mode: 'monthly', year: 2026, startMonth: 1, endMonth: 10 },
        { mode: 'monthly', year: 2026, startMonth: 9, endMonth: 1 },
        { mode: 'custom', customUTC: '2026-02-30T00:00' }, { mode: 'custom', customUTC: '2025-01-01T00:00' }]) {
        assert.throws(() => api.cutoffs(options, now));
    }
});

test('collection sends only financial evidence to server, uses selected host and snapshots twice', async () => {
    let snapshots = 0;
    const { api, calls } = setup((url, options) => {
        if (url === '/api/account-statements') {
            assert.ok(!JSON.stringify(options).includes('ro:test-only'));
            const data = JSON.parse(options.body);
            assert.equal(data.network, 'rh');
            assert.equal(data.snapshot_at_ms, now);
            assert.equal(data.cutoffs[0].timestamp_ms, Date.UTC(2026, 9, 1));
            assert.equal(data.snapshot.index, 42);
            assert.equal(data.snapshot_after.index, 42);
            assert.ok(Array.isArray(data.spot_trades));
            assert.equal(data.deposits.length, 1);
            return response({ success: true, report: { statements: [] } });
        }
        const parsed = new URL(url);
        assert.equal(parsed.origin, 'https://api.rh.lighter.xyz');
        const end = parsed.pathname.split('/').at(-1);
        if (end === 'orderBookDetails') return response({ code: 200, order_book_details: [], spot_order_book_details: [] });
        if (end === 'assetDetails') return response({ code: 200, asset_details: [] });
        assert.equal(options.headers.Authorization, 'ro:test-only');
        if (end === 'account') {
            snapshots++;
            return response({ code: 200, accounts: [{ index: 42, l1_address: '0x' + '1'.repeat(40), assets: [], positions: [], shares: [] }] });
        }
        if (end === 'leases') return response({ code: 200, leases: [], next_cursor: '' });
        if (end === 'trades') return response({ code: 200, trades: [], next_cursor: '' });
        if (parsed.pathname.includes('deposit')) return response({ code: 200, deposits: [{ id: 'deposit1' }], cursor: '' });
        if (parsed.pathname.includes('transfer')) return response({ code: 200, transfers: [], cursor: '' });
        if (parsed.pathname.includes('withdraw')) return response({ code: 200, withdraws: [], cursor: '' });
        throw new Error('Unknown endpoint');
    });
    const result = await api.collect({ apiBase: 'https://api.rh.lighter.xyz', network: 'rh', authToken: 'ro:test-only', accountIndex: 42,
        cutoffs: [{ label: 'September', timestamp_ms: Date.UTC(2026, 9, 1) }] });
    assert.equal(snapshots, 2);
    assert.equal(result.evidence.deposits.length, 1);
    assert.ok(!JSON.stringify(result).includes('ro:test-only'));
    assert.equal(calls.filter(c => c.url === '/api/account-statements').length, 1);
});

test('history failures, rate limits and stuck cursors cannot silently return partial success', async () => {
    let count = 0;
    const opts = { apiBase: 'https://api.rh.lighter.xyz', authToken: 'ro:test-only', accountIndex: 42,
        path: 'deposit/history', key: 'deposits', onProgress: () => {} };
    const failed = setup(() => response(++count === 1 ? { code: 200, deposits: [{}], cursor: 'next' } : {}, count === 1 ? 200 : 500));
    await assert.rejects(failed.api.history(opts), /HTTP 500/);
    const stuck = setup(() => response({ code: 200, deposits: [{}], cursor: 'next' }));
    await assert.rejects(stuck.api.history(opts), /pagination did not advance/);
    const limited = setup(() => response({}, 429));
    await assert.rejects(limited.api.history(opts), /HTTP 429/);
    assert.equal(limited.calls.length, 6);
});

test('JSON downloads include evidence without credentials and report data is cleared on network switch', async () => {
    const blobs = [];
    const downloads = [];
    const { context } = setup();
    context.Blob = Blob;
    context.URL = { createObjectURL: b => { blobs.push(b); return 'blob:test'; }, revokeObjectURL() {} };
    context.document = { body: { appendChild() {} },
        getElementById: () => ({ innerHTML: '', classList: { add() {} } }),
        createElement: () => ({ click() { downloads.push(this.download); }, remove() {} }) };
    context.showStatus = () => {};
    vm.runInContext("statementResults = {42: {report: {network: 'core', statements: []}, evidence: {trades: []}}}", context);
    await vm.runInContext("downloadStatements(42, 'json')", context);
    assert.equal(downloads[0], 'lighter_statements_core_account_42.json');
    assert.deepEqual(JSON.parse(await blobs[0].text()), { network: 'core', statements: [], evidence: { trades: [] } });
    vm.runInContext('clearStatements()', context);
    assert.equal(vm.runInContext('Object.keys(statementResults).length', context), 0);
});
