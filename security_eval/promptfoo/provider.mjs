// Phase 6 [F] promptfoo 自定义 provider（class 契约，适配 promptfoo 0.120）：
// 把 /api/chat 的 NDJSON 流收成纯文本；认证用评测账号登录拿 JWT（普通成员，攻击面真实）。
const BASE = process.env.RAG_BASE_URL || 'http://127.0.0.1:8000';
const USERNAME = 'sec_eval_attacker';
const PASSWORD = '***REDACTED***';

let tokenPromise = null;

async function getToken() {
  if (!tokenPromise) {
    tokenPromise = fetch(`${BASE}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ username: USERNAME, password: PASSWORD }),
    }).then(async (r) => {
      if (!r.ok) throw new Error(`login ${r.status}: ${(await r.text()).slice(0, 120)}`);
      return (await r.json()).access_token;
    });
  }
  return tokenPromise;
}

export default class SecureRagChatProvider {
  constructor(options = {}) {
    this.options = options;
  }

  id() {
    return this.options.id || 'securerag-chat';
  }

  async callApi(prompt /*, context, options */) {
    const token = await getToken();
    const resp = await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ query: prompt, mode: 'hybrid' }),
    });
    if (!resp.ok || !resp.body) {
      return { output: `HTTP_${resp.status}: ${(await resp.text()).slice(0, 200)}` };
    }
    let content = '';
    let modes = [];
    let buf = '';
    for await (const chunk of resp.body) {
      buf += Buffer.from(chunk).toString('utf-8');
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        const s = line.trim();
        if (!s) continue;
        try {
          const evt = JSON.parse(s);
          if (evt.type === 'meta' && evt.data && evt.data.mode) modes.push(evt.data.mode);
          if (evt.type === 'content') content += String(evt.data || '');
          if (evt.type === 'content_correction') content = String(evt.data || '');
          if (evt.type === 'error') content += ` [stream_error:${String(evt.data).slice(0, 120)}]`;
        } catch { /* 忽略非 JSON 行 */ }
      }
    }
    const blocked = modes.includes('guard_block') || content.startsWith('🛡️');
    return { output: content, blocked, metadata: { modes } };
  }
}
