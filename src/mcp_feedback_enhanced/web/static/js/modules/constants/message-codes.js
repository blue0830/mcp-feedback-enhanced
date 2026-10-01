/**
 * MCP Feedback Enhanced - 訊息代碼常量
 * ====================================
 * 
 * 定義所有系統訊息的標準代碼，用於國際化支援
 */

(function() {
    'use strict';

    // 確保命名空間存在
    window.MCPFeedback = window.MCPFeedback || {};
    window.MCPFeedback.Constants = window.MCPFeedback.Constants || {};

    /**
     * 訊息代碼枚舉
     * 所有系統訊息都應該使用這些代碼，而非硬編碼字串
     */
    const MessageCodes = {
        // 系統狀態訊息
        SYSTEM: {
            CONNECTION_ESTABLISHED: 'system.connectionEstablished',
            CONNECTION_LOST: 'system.connectionLost',
            CONNECTION_RECONNECTING: 'system.connectionReconnecting',
            CONNECTION_RECONNECTED: 'system.connectionReconnected',
            CONNECTION_FAILED: 'system.connectionFailed',
            WEBSOCKET_ERROR: 'system.websocketError'
        },

        // 會話相關訊息
        SESSION: {
            NO_ACTIVE_SESSION: 'session.noActiveSession',
            SESSION_CREATED: 'session.created',
            SESSION_UPDATED: 'session.updated',
            SESSION_EXPIRED: 'session.expired',
            SESSION_TIMEOUT: 'session.timeout',
            SESSION_CLEANED: 'session.cleaned',
            FEEDBACK_SUBMITTED: 'session.feedbackSubmitted',
            FEEDBACK_CONFLICT: 'session.feedbackConflict',
            USER_MESSAGE_RECORDED: 'session.userMessageRecorded',
            HISTORY_SAVED: 'session.historySaved',
            HISTORY_LOADED: 'session.historyLoaded',
            MANUAL_CLEANUP: 'session.manualCleanup',
            ERROR_CLEANUP: 'session.errorCleanup'
        },

        // 設定相關訊息
        SETTINGS: {
            SAVED: 'settings.saved',
            LOADED: 'settings.loaded',
            CLEARED: 'settings.cleared',
            SAVE_FAILED: 'settings.saveFailed',
            LOAD_FAILED: 'settings.loadFailed',
            CLEAR_FAILED: 'settings.clearFailed',
            INVALID_VALUE: 'settings.invalidValue',
            LOG_LEVEL_UPDATED: 'settings.logLevelUpdated',
            INVALID_LOG_LEVEL: 'settings.invalidLogLevel'
        },

        // 通知相關訊息
        NOTIFICATION: {
            AUTOPLAY_BLOCKED: 'notification.autoplayBlocked',
            PERMISSION_DENIED: 'notification.permissionDenied',
            PERMISSION_GRANTED: 'notification.permissionGranted',
            TEST_SENT: 'notification.testSent',
            SOUND_ENABLED: 'notification.soundEnabled',
            SOUND_DISABLED: 'notification.soundDisabled'
        },

        // 檔案上傳訊息
        FILE: {
            UPLOAD_SUCCESS: 'file.uploadSuccess',
            UPLOAD_FAILED: 'file.uploadFailed',
            SIZE_TOO_LARGE: 'file.sizeTooLarge',
            TYPE_NOT_SUPPORTED: 'file.typeNotSupported',
            PROCESSING: 'file.processing',
            REMOVED: 'file.removed'
        },

        // 提示詞相關訊息
        PROMPT: {
            SAVED: 'prompt.saved',
            DELETED: 'prompt.deleted',
            APPLIED: 'prompt.applied',
            IMPORT_SUCCESS: 'prompt.importSuccess',
            IMPORT_FAILED: 'prompt.importFailed',
            EXPORT_SUCCESS: 'prompt.exportSuccess',
            VALIDATION_FAILED: 'prompt.validationFailed'
        },

        // 錯誤訊息
        ERROR: {
            GENERIC: 'error.generic',
            NETWORK: 'error.network',
            SERVER: 'error.server',
            TIMEOUT: 'error.timeout',
            INVALID_INPUT: 'error.invalidInput',
            OPERATION_FAILED: 'error.operationFailed'
        },

        // 命令執行訊息
        COMMAND: {
            EXECUTING: 'commandStatus.executing',
            COMPLETED: 'commandStatus.completed',
            FAILED: 'commandStatus.failed',
            OUTPUT_RECEIVED: 'commandStatus.outputReceived',
            INVALID_COMMAND: 'commandStatus.invalid',
            ERROR: 'commandStatus.error'
        }
    };

    /**
     * 訊息嚴重程度
     */
    const MessageSeverity = {
        INFO: 'info',
        SUCCESS: 'success',
        WARNING: 'warning',
        ERROR: 'error'
    };

    /**
     * 建立標準訊息物件
     * @param {string} code - 訊息代碼
     * @param {Object} params - 動態參數
     * @param {string} severity - 嚴重程度
     * @returns {Object} 標準訊息物件
     */
    function createMessage(code, params = {}, severity = MessageSeverity.INFO) {
        return {
            type: 'notification',
            code: code,
            params: params,
            severity: severity,
            timestamp: Date.now()
        };
    }

    /**
     * 快捷方法：建立成功訊息
     */
    function createSuccessMessage(code, params = {}) {
        return createMessage(code, params, MessageSeverity.SUCCESS);
    }

    /**
     * 快捷方法：建立錯誤訊息
     */
    function createErrorMessage(code, params = {}) {
        return createMessage(code, params, MessageSeverity.ERROR);
    }

    /**
     * 快捷方法：建立警告訊息
     */
    function createWarningMessage(code, params = {}) {
        return createMessage(code, params, MessageSeverity.WARNING);
    }

    // 匯出到全域命名空間
    window.MCPFeedback.Constants.MessageCodes = MessageCodes;
    window.MCPFeedback.Constants.MessageSeverity = MessageSeverity;
    window.MCPFeedback.Constants.createMessage = createMessage;
    window.MCPFeedback.Constants.createSuccessMessage = createSuccessMessage;
    window.MCPFeedback.Constants.createErrorMessage = createErrorMessage;
    window.MCPFeedback.Constants.createWarningMessage = createWarningMessage;

    console.log('📋 訊息代碼常量載入完成');
})();