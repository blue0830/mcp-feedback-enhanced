/**
 * MCP Feedback Enhanced - Remote communication settings card
 * ==========================================================
 *
 * Drives the shared card rendered by components/remote-settings-card.html (settings tab
 * of feedback-cli windows and the standalone ``/remote-settings`` page).
 *
 * Responsibilities:
 * - load the redacted remote configuration and show it (the token is never displayed);
 * - save edits, switch the feature on or off and run the connection test, including the
 *   progress polling of the asynchronous test endpoint;
 * - re-render texts generated from script when the language changes.
 *
 * Limitations:
 * - stays idle when the card element is missing (windows hosted by the MCP server path);
 * - the server is the authority for every rule (verification, allowlist, local-origin
 *   guard): this script only reflects its answers and never decides them;
 * - all server-provided strings are written with textContent, never as HTML;
 * - requires remote-common.js.
 */

(function() {
    'use strict';

    window.MCPFeedback = window.MCPFeedback || {};
    const Remote = window.MCPFeedback.Remote;
    if (!Remote) {
        console.error('remote-settings-card.js requires remote-common.js');
        return;
    }

    const API_CONFIG = '/api/remote-config';
    const API_TEST = '/api/remote-config/test';
    const POLL_INTERVAL_MS = 1000;
    // Consecutive failed polls before the page stops waiting for the test result.
    const MAX_POLL_FAILURES = 5;
    // Used when the server does not report how long the reply wait lasts.
    const DEFAULT_REPLY_SECONDS = 120;
    const STEP_ICONS = { pending: '○', running: '⏳', ok: '✓', failed: '✗', skipped: '–' };

    /**
     * Failed request: ``status`` is the HTTP status (0 when the server is unreachable),
     * ``code`` the machine-readable error key used for localized text.
     */
    class RemoteRequestError extends Error {
        constructor(status, code, data) {
            super(code);
            this.status = status;
            this.code = code;
            this.data = data;
        }
    }

    async function requestJson(method, url, body) {
        const options = { method: method, cache: 'no-store', headers: {} };
        if (method !== 'GET') {
            // The server only accepts JSON writes; this also blocks cross-site form posts.
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body === undefined ? {} : body);
        }

        let response;
        try {
            response = await fetch(url, options);
        } catch (error) {
            throw new RemoteRequestError(0, 'request_failed', null);
        }

        let data = null;
        try {
            data = await response.json();
        } catch (error) {
            data = null;
        }

        if (!response.ok) {
            let code = data && typeof data.error === 'string' ? data.error : 'request_failed';
            if (response.status === 404 && code === 'not_found') {
                // A 404 from the remote endpoints means this window is not CLI-hosted.
                code = 'unavailable';
            }
            throw new RemoteRequestError(response.status, code, data);
        }
        return data;
    }

    function sleep(milliseconds) {
        return new Promise(function(resolve) {
            setTimeout(resolve, milliseconds);
        });
    }

    function makeSpan(className, text) {
        const span = document.createElement('span');
        span.className = className;
        span.textContent = text;
        return span;
    }

    class RemoteSettingsCard {
        constructor(root) {
            this.root = root;
            this.el = {
                verification: root.querySelector('#remoteVerification'),
                toggle: root.querySelector('#remoteEnableToggle'),
                provider: root.querySelector('#remoteProvider'),
                token: root.querySelector('#remoteToken'),
                channel: root.querySelector('#remoteForumChannelId'),
                users: root.querySelector('#remoteAllowedUsers'),
                save: root.querySelector('#remoteSaveBtn'),
                test: root.querySelector('#remoteTestBtn'),
                message: root.querySelector('#remoteMessage'),
                checkPanel: root.querySelector('#remoteCheckPanel'),
                checkSteps: root.querySelector('#remoteCheckSteps'),
                checkHint: root.querySelector('#remoteCheckHint'),
                checkResult: root.querySelector('#remoteCheckResult')
            };

            // Last redacted configuration received from the server.
            this.config = null;
            // True once a field was edited and not yet saved.
            this.dirty = false;
            // True while a request or a connection test is in flight (controls disabled).
            this.busy = false;
            // Message under the buttons: {key, kind, reasonCode, params}; kept as data so
            // it can be re-rendered in another language.
            this.message = null;
            // Last connection test payload (also re-rendered on language change).
            this.check = null;
            this.pollFailed = false;
            // Client-side clock for the reply countdown (started when the step starts).
            this.replyStartedAt = null;
            this.countdownTimer = null;

            this.bindEvents();
            this.refreshTexts();
            this.load();
        }

        bindEvents() {
            const self = this;
            const markDirty = function() {
                self.dirty = true;
            };
            ['provider', 'token', 'channel', 'users'].forEach(function(name) {
                self.el[name].addEventListener('input', markDirty);
                self.el[name].addEventListener('change', markDirty);
            });

            this.el.save.addEventListener('click', function() {
                self.run(function() { return self.saveFields(true); });
            });
            this.el.test.addEventListener('click', function() {
                self.run(function() { return self.runCheck(); });
            });
            this.el.toggle.addEventListener('click', function() {
                self.run(function() { return self.toggle(); });
            });
        }

        // ---- actions -------------------------------------------------------------------

        /**
         * Run one user action with the controls locked; failures become a message and the
         * card is re-synced with the server so it never shows a state that did not apply.
         */
        async run(action) {
            if (this.busy) {
                return;
            }
            this.setBusy(true);
            try {
                await action();
            } catch (error) {
                this.showError(error);
                await this.load({ keepMessage: true });
            } finally {
                this.setBusy(false);
            }
        }

        async load(options) {
            try {
                const data = await requestJson('GET', API_CONFIG);
                this.applyConfig(data.config);
            } catch (error) {
                if (!(options && options.keepMessage)) {
                    this.setMessage('remoteChannel.messages.loadFailed', 'error', { reasonCode: error.code || 'request_failed' });
                }
            }
        }

        collectPayload() {
            const discord = {
                forum_channel_id: this.el.channel.value.trim(),
                // The server accepts a comma/space separated string and validates each id.
                allowed_user_ids: this.el.users.value.trim()
            };
            // An empty token means "keep the stored one": it is simply not sent.
            const token = this.el.token.value.trim();
            if (token) {
                discord.token = token;
            }
            return { provider: this.el.provider.value, discord: discord };
        }

        async saveFields(announce) {
            const wasVerified = Boolean(this.config && this.config.verified);
            const data = await requestJson('POST', API_CONFIG, this.collectPayload());
            this.applyConfig(data.config);
            if (announce) {
                const lostVerification = wasVerified && !data.config.verified;
                this.setMessage(
                    lostVerification ? 'remoteChannel.messages.savedNeedsTest' : 'remoteChannel.messages.saved',
                    lostVerification ? 'warning' : 'success'
                );
            }
        }

        async toggle() {
            const wanted = !(this.config && this.config.enabled);
            // Unsaved edits are saved first; the server then decides whether the
            // verification survived them.
            if (this.dirty) {
                await this.saveFields(false);
            }
            const data = await requestJson('POST', API_CONFIG, { enabled: wanted });
            this.applyConfig(data.config);
            this.setMessage(
                wanted ? 'remoteChannel.messages.enabled' : 'remoteChannel.messages.disabled',
                'success'
            );
        }

        async runCheck() {
            // The server tests the stored settings, so pending edits must be stored first.
            if (this.dirty) {
                await this.saveFields(false);
            }
            this.setMessage(null);
            this.pollFailed = false;

            let run;
            try {
                run = await requestJson('POST', API_TEST, {});
            } catch (error) {
                if (error.status === 409 && error.data && error.data.run_id) {
                    // A test is already running (another window or a reload): follow it.
                    run = await requestJson('GET', API_TEST + '/' + encodeURIComponent(error.data.run_id));
                } else {
                    throw error;
                }
            }

            this.showCheck(run);
            await this.pollCheck(run.run_id);
            // The test may have stored a new verification: show the real state.
            await this.load();
        }

        async pollCheck(runId) {
            let failures = 0;
            while (this.check && !this.check.done) {
                await sleep(POLL_INTERVAL_MS);
                try {
                    const payload = await requestJson('GET', API_TEST + '/' + encodeURIComponent(runId));
                    failures = 0;
                    this.showCheck(payload);
                } catch (error) {
                    failures += 1;
                    // Unknown run or refused access will not get better by retrying.
                    if (failures >= MAX_POLL_FAILURES || error.status === 404 || error.status === 403) {
                        this.pollFailed = true;
                        this.stopCountdown();
                        this.setMessage('remoteChannel.check.pollFailed', 'error');
                        return;
                    }
                }
            }
        }

        // ---- state -> view -------------------------------------------------------------

        setBusy(busy) {
            this.busy = busy;
            this.root.setAttribute('aria-busy', busy ? 'true' : 'false');
            ['toggle', 'provider', 'token', 'channel', 'users', 'save', 'test'].forEach(
                function(name) {
                    this.el[name].disabled = busy;
                },
                this
            );
        }

        applyConfig(config) {
            this.config = config;
            this.el.provider.value = config.provider;
            this.el.channel.value = config.discord.forum_channel_id || '';
            this.el.users.value = (config.discord.allowed_user_ids || []).join(', ');
            // The token never comes back from the server: the field always starts empty.
            this.el.token.value = '';
            this.dirty = false;
            this.renderPlaceholders();
            this.renderToggle();
            this.renderVerification();
        }

        setMessage(key, kind, options) {
            this.message = key
                ? {
                    key: key,
                    kind: kind || 'info',
                    reasonCode: options && options.reasonCode,
                    params: options && options.params
                }
                : null;
            this.renderMessage();
        }

        showError(error) {
            const code = error && error.code ? error.code : 'request_failed';
            this.setMessage('remoteChannel.messages.failed', 'error', { reasonCode: code });
        }

        showCheck(payload) {
            const wasDone = Boolean(this.check && this.check.done);
            this.check = payload;
            const replying = payload.steps.some(function(step) {
                return step.id === 'reply' && step.state === 'running';
            });
            let promptAppears = false;
            if (replying && this.replyStartedAt === null) {
                this.startCountdown();
                promptAppears = true;
            } else if (!replying) {
                this.stopCountdown();
            }
            this.renderCheck();
            // Scroll only on the two moments that matter: the "reply in Discord now" prompt
            // appears, and the final result arrives. Scrolling on every poll would fight the user.
            if (promptAppears || (payload.done && !wasDone)) {
                this.revealCheckPanel();
            }
        }

        /**
         * Bring the connection test panel into view.
         *
         * The card is taller than a default desktop window (and the standalone page often is
         * too), so the prompt telling the user to reply in Discord would sit below the fold.
         * "nearest" scrolls only as far as needed and never moves an already visible panel.
         */
        revealCheckPanel() {
            const panel = this.el.checkPanel;
            if (panel && !panel.hidden && typeof panel.scrollIntoView === 'function') {
                panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            }
        }

        startCountdown() {
            const self = this;
            this.replyStartedAt = Date.now();
            this.stopCountdownTimerOnly();
            this.countdownTimer = setInterval(function() {
                self.renderCheckHint();
            }, 1000);
        }

        stopCountdownTimerOnly() {
            if (this.countdownTimer !== null) {
                clearInterval(this.countdownTimer);
                this.countdownTimer = null;
            }
        }

        stopCountdown() {
            this.stopCountdownTimerOnly();
            this.replyStartedAt = null;
        }

        // ---- rendering -----------------------------------------------------------------

        /**
         * Re-render every text generated from script (called after a language switch).
         */
        refreshTexts() {
            this.renderPlaceholders();
            this.renderVerification();
            this.renderMessage();
            this.renderCheck();
        }

        renderPlaceholders() {
            const tokenSet = Boolean(this.config && this.config.discord.token_set);
            this.el.token.placeholder = Remote.t(
                tokenSet ? 'remoteChannel.token.placeholderSet' : 'remoteChannel.token.placeholder'
            );
        }

        renderToggle() {
            const enabled = Boolean(this.config && this.config.enabled);
            this.el.toggle.classList.toggle('active', enabled);
            this.el.toggle.setAttribute('aria-checked', enabled ? 'true' : 'false');
        }

        renderVerification() {
            const config = this.config;
            const element = this.el.verification;
            if (!config) {
                element.textContent = '';
                return;
            }
            const discord = config.discord;
            const complete = Boolean(
                discord.token_set && discord.forum_channel_id && discord.allowed_user_ids.length > 0
            );

            let state;
            if (config.effective) {
                state = 'effective';
            } else if (config.verified) {
                state = 'verified';
            } else if (complete) {
                state = 'unverified';
            } else {
                state = 'incomplete';
            }
            const keys = {
                effective: 'remoteChannel.verification.effective',
                verified: 'remoteChannel.verification.verifiedDisabled',
                unverified: 'remoteChannel.verification.unverified',
                incomplete: 'remoteChannel.verification.incomplete'
            };
            element.dataset.state = state;
            element.textContent = Remote.t(keys[state]);
        }

        renderMessage() {
            const element = this.el.message;
            if (!this.message) {
                element.textContent = '';
                return;
            }
            const params = Object.assign({}, this.message.params);
            if (this.message.reasonCode) {
                params.reason = Remote.reasonText(this.message.reasonCode);
            }
            element.dataset.kind = this.message.kind;
            element.textContent = Remote.t(this.message.key, params);
        }

        stepLabel(stepId) {
            const key = 'remoteChannel.check.steps.' + stepId;
            const text = Remote.t(key);
            return text === key ? stepId : text;
        }

        renderCheck() {
            const check = this.check;
            if (!check) {
                this.el.checkPanel.hidden = true;
                return;
            }
            this.el.checkPanel.hidden = false;

            const list = this.el.checkSteps;
            list.textContent = '';
            check.steps.forEach(function(step) {
                const item = document.createElement('li');
                item.className = 'remote-check-step';
                item.dataset.state = step.state;
                item.appendChild(makeSpan('remote-check-icon', STEP_ICONS[step.state] || STEP_ICONS.pending));
                item.appendChild(makeSpan('remote-check-label', this.stepLabel(step.id)));
                if (step.state === 'failed' && step.reason) {
                    item.appendChild(makeSpan('remote-check-reason', Remote.reasonText(step.reason)));
                } else if (step.state === 'ok' && step.detail) {
                    // Bot or channel name reported by Discord (shown as plain text only).
                    item.appendChild(makeSpan('remote-check-detail', step.detail));
                }
                list.appendChild(item);
            }, this);

            this.renderCheckHint();
            this.renderCheckResult();
        }

        renderCheckHint() {
            const check = this.check;
            const element = this.el.checkHint;
            const replying = Boolean(check && check.steps.some(function(step) {
                return step.id === 'reply' && step.state === 'running';
            }));
            if (!replying) {
                element.hidden = true;
                return;
            }
            const total = check.reply_timeout_seconds || DEFAULT_REPLY_SECONDS;
            const elapsed = this.replyStartedAt === null ? 0 : Math.floor((Date.now() - this.replyStartedAt) / 1000);
            element.textContent = Remote.t('remoteChannel.check.replyPrompt', {
                seconds: Math.max(0, total - elapsed)
            });
            element.hidden = false;
        }

        renderCheckResult() {
            const check = this.check;
            const element = this.el.checkResult;
            if (!check) {
                element.hidden = true;
                return;
            }

            let kind = 'info';
            let text;
            if (!check.done) {
                text = Remote.t('remoteChannel.check.running');
            } else if (check.passed && check.verified) {
                kind = 'success';
                text = Remote.t('remoteChannel.check.passed');
            } else if (check.passed) {
                kind = 'warning';
                text = Remote.t(
                    check.verify_error === 'config_changed'
                        ? 'remoteChannel.check.passedNotSaved'
                        : 'remoteChannel.check.passedSaveFailed'
                );
            } else {
                const failed = check.steps.find(function(step) {
                    return step.state === 'failed';
                });
                kind = 'error';
                text = Remote.t('remoteChannel.check.failed', {
                    step: failed ? this.stepLabel(failed.id) : '',
                    reason: Remote.reasonText(failed && failed.reason ? failed.reason : 'internal_error')
                });
            }
            element.dataset.kind = kind;
            element.textContent = text;
            element.hidden = false;
        }
    }

    function init() {
        const root = document.getElementById('remoteSettingsCard');
        if (!root || root.dataset.remoteInitialized === '1') {
            return;
        }
        root.dataset.remoteInitialized = '1';
        Remote.register(new RemoteSettingsCard(root));
    }

    window.MCPFeedback.RemoteSettingsCard = RemoteSettingsCard;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
