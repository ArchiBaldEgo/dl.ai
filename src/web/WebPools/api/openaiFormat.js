const { randomUUID } = require('crypto');

function nowUnix() {
    return Math.floor(Date.now() / 1000);
}

function makeChatCompletion({ model = 'gpt-4o', content, usage }) {
    return {
        id: `chatcmpl_${randomUUID().replaceAll('-', '')}`,
        object: 'chat.completion',
        created: nowUnix(),
        model,
        choices: [
            {
                index: 0,
                message: { role: 'assistant', content: content ?? '' },
                finish_reason: 'stop',
            },
        ],
        usage: usage ?? { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    };
}

function makeOpenAIError({ message, code = 'server_busy', type = 'server_error' }) {
    return {
        error: {
            message,
            type,
            param: null,
            code,
        },
    };
}

module.exports = { makeChatCompletion, makeOpenAIError };
