/**
 * MCP Feedback Enhanced - Remote communication shared helpers
 * ===========================================================
 *
 * Responsibilities:
 * - translation helpers for the remote communication modules (``t`` and ``reasonText``);
 * - a small registry so the language switcher can ask every remote component to
 *   re-render the texts it generated from script (``refreshTexts``, called by i18n.js).
 *
 * Limitations:
 * - depends only on ``window.i18nManager`` (optional: keys are shown as-is without it);
 * - must be loaded before the other remote modules.
 */

(function() {
    'use strict';

    window.MCPFeedback = window.MCPFeedback || {};

    const components = [];

    /**
     * Translate a key; returns the key itself while translations are not loaded.
     */
    function t(key, params) {
        const manager = window.i18nManager;
        if (manager && typeof manager.t === 'function') {
            return manager.t(key, params || {});
        }
        return key;
    }

    /**
     * Localized text for a machine-readable reason code (status badge, check steps and
     * server errors share one dictionary); unknown codes fall back to a generic text.
     */
    function reasonText(code) {
        const key = 'remoteChannel.reasons.' + code;
        const text = t(key);
        if (text !== key) {
            return text;
        }
        return t('remoteChannel.reasons.unknown', { code: String(code) });
    }

    /**
     * Register a component that implements ``refreshTexts()``.
     */
    function register(component) {
        components.push(component);
    }

    /**
     * Ask every registered component to re-render its generated texts.
     */
    function refreshTexts() {
        components.forEach(function(component) {
            try {
                component.refreshTexts();
            } catch (error) {
                console.error('Remote component text refresh failed:', error);
            }
        });
    }

    window.MCPFeedback.Remote = {
        t: t,
        reasonText: reasonText,
        register: register,
        refreshTexts: refreshTexts
    };
})();
