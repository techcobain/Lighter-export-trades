// Credentials stay in browser headers. Only snapshots and source records go to the app server.
const LighterAccountStatements = (() => {
    const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
    const GENESIS_MS = Date.UTC(2025, 0, 17);

    function cutoffs({ mode, year, startMonth = 1, endMonth = 12, customUTC }, now = Date.now()) {
        const result = [];
        if (mode === 'custom') {
            // datetime-local inputs are explicitly interpreted as UTC, not local time.
            if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(customUTC || '')) {
                throw new Error('Choose a custom UTC date and time.');
            }
            const timestamp = Date.parse(`${customUTC}Z`);
            if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, customUTC.length) !== customUTC) {
                throw new Error('Choose a valid custom UTC date and time.');
            }
            result.push({ label: 'Custom balance statement', timestamp_ms: timestamp });
        } else {
            year = Number(year);
            if (!Number.isInteger(year) || year < 2025 || year > 2099) throw new Error('Choose a valid year.');
            if (mode === 'annual') {
                result.push({ label: `${year} year-end statement`, timestamp_ms: Date.UTC(year + 1, 0, 1) });
            } else if (mode === 'monthly') {
                startMonth = Number(startMonth); endMonth = Number(endMonth);
                if (!Number.isInteger(startMonth) || !Number.isInteger(endMonth) || startMonth < 1 || endMonth > 12 || startMonth > endMonth) {
                    throw new Error('Choose a valid month range.');
                }
                for (let month = startMonth; month <= endMonth; month++) {
                    const label = new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
                    result.push({ label: `${label} month-end statement`, timestamp_ms: Date.UTC(year, month, 1) });
                }
            } else throw new Error('Choose a statement type.');
        }
        if (result.some(item => !Number.isFinite(item.timestamp_ms) || item.timestamp_ms < GENESIS_MS || item.timestamp_ms > now)) {
            throw new Error('Statement dates must be between January 17, 2025 and now. Future month/year ends are unavailable.');
        }
        return result;
    }

    async function requestJSON(url, options, onProgress) {
        for (let attempt = 0; attempt <= 5; attempt++) {
            const response = await fetch(url, options);
            if ([429, 405].includes(response.status) && attempt < 5) {
                const delay = Math.min(Number(response.headers.get('Retry-After')) * 1000 || 15000, 60000);
                onProgress(`Rate limited, waiting ${Math.ceil(delay / 1000)}s...`);
                await wait(delay);
                continue;
            }
            if (!response.ok) {
                if (url === '/api/account-statements' && response.status === 400) {
                    const error = await response.json();
                    throw new Error(typeof error.detail === 'string' ? error.detail : 'Statement source data is invalid.');
                }
                throw new Error(`Statement data request failed (HTTP ${response.status}). Please retry.`);
            }
            const data = await response.json();
            if (url.startsWith('https://') && data.code !== 200) throw new Error('Lighter could not return statement data. Please retry.');
            return data;
        }
    }

    async function snapshot(apiBase, authToken, accountIndex, onProgress) {
        const params = new URLSearchParams({ by: 'index', value: accountIndex, active_only: 'false' });
        const data = await requestJSON(`${apiBase}/api/v1/account?${params}`, { headers: { Authorization: authToken } }, onProgress);
        if (data.next_cursor || !Array.isArray(data.accounts) || data.accounts.length !== 1
            || Number(data.accounts[0].index) !== accountIndex) {
            throw new Error('Could not obtain a complete snapshot for this account.');
        }
        const account = data.accounts[0];
        if (!Array.isArray(account.assets) || !Array.isArray(account.positions) || !Array.isArray(account.shares)) {
            throw new Error('Account snapshot is incomplete.');
        }
        return account;
    }

    async function history({ apiBase, authToken, accountIndex, path, key, params = {}, onProgress }) {
        const rows = [];
        const seen = new Set();
        let cursor = null;
        let page = 0;
        while (true) {
            const query = new URLSearchParams({ account_index: accountIndex, ...params });
            if (cursor) query.set('cursor', cursor);
            onProgress(`Account #${accountIndex}: Fetching ${key} page ${++page}...`);
            const data = await requestJSON(`${apiBase}/api/v1/${path}?${query}`, { headers: { Authorization: authToken } }, onProgress);
            if (!Array.isArray(data[key])) throw new Error(`Incomplete ${key} history.`);
            rows.push(...data[key]);
            const next = data.next_cursor || data.cursor;
            if (!next || !data[key].length) break;
            if (seen.has(next)) throw new Error(`${key} pagination did not advance.`);
            seen.add(next); cursor = next;
            await wait(1000);
        }
        return rows;
    }

    async function collect({ apiBase, network, authToken, accountIndex, cutoffs: requestedCutoffs, onProgress = () => {} }) {
        onProgress(`Account #${accountIndex}: Capturing current balances...`);
        const before = await snapshot(apiBase, authToken, accountIndex, onProgress);
        const capturedAt = Date.now();
        const metadata = await requestJSON(`${apiBase}/api/v1/orderBookDetails`, {}, onProgress);
        const assets = await requestJSON(`${apiBase}/api/v1/assetDetails`, {}, onProgress);
        if (!Array.isArray(metadata.order_book_details) || !Array.isArray(metadata.spot_order_book_details)
            || !Array.isArray(assets.asset_details)) throw new Error('Statement market/asset catalog is incomplete.');
        // Fetch full history so the opening balance can be checked, including positions carried from earlier years.
        const options = { apiBase, authToken, accountIndex, fromTimestamp: GENESIS_MS, toTimestamp: capturedAt, onProgress };
        const trades = await LighterTradeExport.fetchTrades(options);
        await wait(3500);
        const fundings = await LighterTradeExport.fetchFundings(options);
        const wallet = { apiBase, authToken, accountIndex, onProgress };
        await wait(1000);
        // Supplement rounded CSV fees with native spot fee ticks, when available.
        const spotTrades = await history({ ...wallet, path: 'trades', key: 'trades', params: {
            market_type: 'spot', sort_by: 'timestamp', sort_dir: 'desc', limit: 100, aggregate: 'false',
        } });
        await wait(1000);
        const perpTrades = await history({ ...wallet, path: 'trades', key: 'trades', params: {
            market_type: 'perp', sort_by: 'timestamp', sort_dir: 'desc', limit: 100, aggregate: 'false',
        } });
        const deposits = await history({ ...wallet, path: 'deposit/history', key: 'deposits', params: { l1_address: before.l1_address } });
        await wait(1000);
        const transfers = await history({ ...wallet, path: 'transfer/history', key: 'transfers', params: { type: 'all' } });
        await wait(1000);
        const withdrawals = await history({ ...wallet, path: 'withdraw/history', key: 'withdraws' });
        await wait(1000);
        const leases = await history({ ...wallet, path: 'leases', key: 'leases', params: { limit: 100 } });
        const outflowChecks = [];
        const dayMS = 86400000;
        const withdrawalDays = [...new Set(withdrawals.filter(row => row.type === 'fast' && row.status === 'completed')
            .map(row => Math.floor(Number(row.timestamp) / dayMS) * dayMS))];
        for (const day of withdrawalDays) {
            if (day + dayMS > capturedAt) continue;
            onProgress(`Account #${accountIndex}: Checking historical withdrawal charges...`);
            const query = new URLSearchParams({ by: 'index', value: accountIndex, resolution: '1d',
                start_timestamp: day / 1000, end_timestamp: (day + dayMS) / 1000,
                count_back: 0, ignore_transfers: 'false' });
            try {
                const data = await requestJSON(`${apiBase}/api/v1/pnl?${query}`, { headers: { Authorization: authToken } }, onProgress);
                outflowChecks.push({ day_ms: day, pnl: data.pnl || [] });
            } catch {
                // Missing independent fee evidence leaves a draft, never an assumed fee.
                outflowChecks.push({ day_ms: day, pnl: [] });
            }
            await wait(1000);
        }
        onProgress(`Account #${accountIndex}: Checking snapshot consistency...`);
        const after = await snapshot(apiBase, authToken, accountIndex, onProgress);
        const dataset = {
            network, account_index: accountIndex, snapshot_at_ms: capturedAt, snapshot: before, snapshot_after: after,
            cutoffs: requestedCutoffs, asset_details: assets.asset_details,
            markets: [...metadata.order_book_details, ...metadata.spot_order_book_details],
            trades: trades.raw_trades, spot_trades: spotTrades, perp_trades: perpTrades, outflow_checks: outflowChecks,
            fundings: fundings.raw_fundings, deposits, transfers, withdrawals, leases,
        };
        onProgress(`Account #${accountIndex}: Reconstructing balances and loading historical prices...`);
        const result = await requestJSON('/api/account-statements', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(dataset),
        }, onProgress);
        if (!result.success || !Array.isArray(result.report?.statements)) throw new Error('Statement generation failed.');
        return { report: result.report, evidence: dataset };
    }

    return { cutoffs, collect, history };
})();

let statementResults = {};

function initializeStatements() {
    const now = new Date();
    document.getElementById('statementYear').value = now.getUTCFullYear();
    const lastClosed = now.getUTCMonth();
    if (!lastClosed) document.getElementById('statementYear').value = now.getUTCFullYear() - 1;
    document.getElementById('statementEndMonth').value = lastClosed || 12;
    document.getElementById('statementCustomUTC').value = now.toISOString().slice(0, 16);
    updateStatementMode();
}

function updateStatementMode() {
    const mode = document.getElementById('statementMode').value;
    document.getElementById('statementYearGroup').classList.toggle('hidden', mode === 'custom');
    document.getElementById('statementMonthRange').classList.toggle('hidden', mode !== 'monthly');
    document.getElementById('statementCustomGroup').classList.toggle('hidden', mode !== 'custom');
    if (mode === 'annual' && Number(document.getElementById('statementYear').value) === new Date().getUTCFullYear()) {
        document.getElementById('statementYear').value = new Date().getUTCFullYear() - 1;
    }
}

function clearStatements() {
    statementResults = {};
    document.getElementById('statementsCard')?.classList.add('hidden');
    const content = document.getElementById('statementContents');
    if (content) content.innerHTML = '';
}

async function generateStatements() {
    const accounts = getSelectedAccounts();
    if (!readOnlyToken || !accounts.length) {
        showStatus('fetchStatus', 'Connect a read-only token and select at least one account.', 'error');
        return;
    }
    let cutoffs;
    try {
        cutoffs = LighterAccountStatements.cutoffs({
            mode: document.getElementById('statementMode').value, year: document.getElementById('statementYear').value,
            startMonth: document.getElementById('statementStartMonth').value,
            endMonth: document.getElementById('statementEndMonth').value,
            customUTC: document.getElementById('statementCustomUTC').value,
        });
    } catch (error) { showStatus('fetchStatus', error.message, 'error'); return; }
    clearStatements();
    setAllFetchButtonsDisabled(true);
    const button = document.getElementById('generateStatementsBtn');
    button.innerHTML = '<span class="spinner"></span>Generating...';
    try {
        const results = {};
        for (const accountIndex of accounts) {
            results[accountIndex] = await LighterAccountStatements.collect({
                apiBase: LIGHTER_API, network: selectedNetwork, authToken: readOnlyToken, accountIndex, cutoffs,
                onProgress: message => showStatus('fetchStatus', message, 'loading'),
            });
        }
        statementResults = results;
        displayStatements();
        const review = Object.values(results).some(({ report }) => report.statements.some(s => s.status === 'review_required'));
        showStatus('fetchStatus', review ? 'Statements generated. Some items need review; see the report notes.' : 'Statements generated and opening balances reconciled within source precision.', review ? 'error' : 'success');
    } catch (error) {
        showStatus('fetchStatus', error.message, 'error');
    } finally {
        setAllFetchButtonsDisabled(false);
        button.textContent = 'Generate Statements';
    }
}

function displayStatements() {
    const container = document.getElementById('statementContents');
    container.innerHTML = '';
    for (const [account, { report }] of Object.entries(statementResults)) {
        const block = document.createElement('div');
        const safeAccount = Number(account);
        const warnings = [...new Set([...report.warnings, ...report.statements.flatMap(s => s.warnings)])];
        block.innerHTML = `<div class="results-header"><span class="results-stats">Account #${safeAccount} · ${escapeHtml(report.network.toUpperCase())}</span>
            <div class="btn-group">${['pdf', 'csv', 'json'].map(format => `<button class="btn-secondary btn-small" onclick="downloadStatements(${safeAccount}, '${format}')">${format.toUpperCase()}</button>`).join('')}</div></div>
            ${warnings.length ? `<div class="info-note">${warnings.map(w => escapeHtml(w)).join('<br>')}</div>` : ''}
            <div class="results-box"><table class="trades-table"><thead><tr><th>Statement</th><th>As of UTC</th><th>Known assets (USD)</th><th>Equity (USD)</th><th>Check</th></tr></thead><tbody>
            ${report.statements.map(s => `<tr><td>${escapeHtml(s.label)}</td><td>${escapeHtml(s.as_of_utc)}</td><td>${escapeHtml(s.known_assets_usd)}</td><td>${escapeHtml(s.account_equity_usd ?? 'Needs review')}</td><td>${s.status === 'reconciled' ? 'Reconciled' : 'Needs review'}</td></tr>`).join('')}
            </tbody></table></div>`;
        container.appendChild(block);
    }
    document.getElementById('statementsCard').classList.remove('hidden');
}

async function downloadStatements(account, format) {
    const data = statementResults[account];
    if (!data) return;
    try {
        let blob;
        if (format === 'json') {
            blob = new Blob([JSON.stringify({ ...data.report, evidence: data.evidence }, null, 2)], { type: 'application/json' });
        } else {
            const response = await fetch('/api/account-statements/export', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ report: data.report, format }),
            });
            if (!response.ok) throw new Error(`Statement download failed (HTTP ${response.status}).`);
            blob = await response.blob();
        }
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `lighter_statements_${data.report.network}_account_${account}.${format}`;
        document.body.appendChild(link); link.click();
        setTimeout(() => { URL.revokeObjectURL(url); link.remove(); }, 100);
    } catch (error) { showStatus('fetchStatus', error.message, 'error'); }
}
