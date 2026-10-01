/**
 * MCP Feedback Enhanced - Remote communication status badge
 * =========================================================
 *
 * Shows the state of the remote channel for the current feedback session in the top bar:
 * off / connecting / waiting / unavailable (with reason) / answered remotely / answered
 * locally. Status updates arrive as ``remote_status`` WebSocket messages.
 *
 * Responsibilities:
 * - render one status payload ``{state, provider, reason, detail}`` as localized text;
 * - keep the last payload so a language switch can re-render it.
 *
 * Limitations:
 * - the badge element only exists in windows hosted by ``feedback-cli``; ``attach()``
 *   returns null elsewhere and nothing is initialized;
 * - ``detail`` is English diagnostics and is never shown (only the localized reason);
 * - requires remote-common.js.
 */

(function() {
    'use strict';

    window.MCPFeedback = window.MCPFeedback || {};
    const Remote = window.MCPFeedback.Remote;
    if (!Remote) {
        console.error('remote-status-badge.js requires remote-common.js');
        return;
    }

    const STATES = ['off', 'connecting', 'waiting', 'unavailable', 'answered_remotely', 'answered_locally'];

    class RemoteStatusBadge {
        /**
         * @param {HTMLElement} element the badge button
         * @param {{onActivate?: Function}} options click handler (opens the settings tab)
         */
        constructor(element, options) {
            this.element = element;
            this.textElement = element.querySelector('.remote-status-text');
            this.status = { state: 'off' };
            this.onActivate = options && typeof options.onActivate === 'function' ? options.onActivate : null;

            const self = this;
            element.addEventListener('click', function() {
                if (self.onActivate) {
                    self.onActivate();
                }
            });
            this.render();
        }

        /**
         * Apply a status payload pushed by the server.
         */
        update(status) {
            this.status = status && typeof status === 'object' ? status : { state: 'off' };
            this.render();
        }

        refreshTexts() {
            this.render();
        }

        render() {
            const status = this.status;
            const state = STATES.indexOf(status.state) !== -1 ? status.state : 'off';

            let text;
            if (state === 'off' && status.reason) {
                text = Remote.t('remoteChannel.status.offWithReason', { reason: Remote.reasonText(status.reason) });
            } else if (state === 'unavailable') {
                text = Remote.t('remoteChannel.status.unavailable', {
                    reason: Remote.reasonText(status.reason || 'internal_error')
                });
            } else {
                text = Remote.t('remoteChannel.status.' + state);
            }

            this.element.dataset.state = state;
            if (this.textElement) {
                this.textElement.textContent = text;
            }
            // The badge can be truncated by the top bar: the tooltip carries the full text.
            this.element.title = text + ' · ' + Remote.t('remoteChannel.status.tooltip');
        }

        /**
         * Bind to ``#remoteStatusBadge`` when present; returns null otherwise.
         */
        static attach(options) {
            const element = document.getElementById('remoteStatusBadge');
            if (!element) {
                return null;
            }
            const badge = new RemoteStatusBadge(element, options);
            Remote.register(badge);
            return badge;
        }
    }

    window.MCPFeedback.RemoteStatusBadge = RemoteStatusBadge;
})();
