/**
 * MCP Feedback Enhanced - Standalone remote settings page bootstrap
 * =================================================================
 *
 * Starts translations for ``/remote-settings`` (opened by ``feedback-cli --remote-settings``).
 *
 * Responsibilities:
 * - load translations and apply the language stored in the general settings;
 * - keep the window title localized.
 *
 * Limitations:
 * - the stored language is only read: nothing is ever written back to the general
 *   settings, so this page cannot clobber them;
 * - no session, WebSocket or feedback module is involved (the card module does the work);
 * - requires i18n.js and remote-common.js.
 */

(function() {
    'use strict';

    const SUPPORTED_LANGUAGES = ['zh-TW', 'zh-CN', 'en'];

    async function readStoredLanguage() {
        try {
            const response = await fetch('/api/load-settings', { cache: 'no-store' });
            if (!response.ok) {
                return null;
            }
            const settings = await response.json();
            const language = settings && settings.language;
            return SUPPORTED_LANGUAGES.indexOf(language) !== -1 ? language : null;
        } catch (error) {
            return null;
        }
    }

    function updateTitle() {
        const Remote = window.MCPFeedback && window.MCPFeedback.Remote;
        if (!Remote) {
            return;
        }
        const key = 'remoteChannel.standalone.title';
        const text = Remote.t(key);
        if (text !== key) {
            document.title = text;
        }
    }

    async function start() {
        const manager = window.i18nManager;
        if (!manager) {
            return;
        }
        try {
            await manager.init();
            const language = await readStoredLanguage();
            if (language && language !== manager.getCurrentLanguage()) {
                manager.setLanguage(language);
            }
        } catch (error) {
            console.error('Remote settings page translation setup failed:', error);
        }
        updateTitle();
        // Re-apply the title whenever the language changes (the card refreshes itself).
        window.MCPFeedback.Remote.register({ refreshTexts: updateTitle });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
