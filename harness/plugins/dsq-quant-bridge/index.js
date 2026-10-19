'use strict'
/**
 * dsq-quant-bridge（固化版 v16 · 面板独立）— :3080 /quantapi/* 量化桥 API
 *
 * ★面板独立铁律（2026-08-16 终版）：
 *   控制页的「删除/批量删除/一键清空」只写本面板的本地隐藏名单
 *   （$DSH_HOME/quantapi_archived.json），绝不调用 workspaceRegistry.archiveSession
 *   —— GUI（:3080）与它的 workspace、会话列表、聊天历史完全不受影响。
 *   恢复：POST /quantapi/restore-all 清空隐藏名单，会话全部重现。
 *
 * 锁死保护：标题含「主系统」的会话不可删除（核心会话）。
 * 固化位置：profiles/web/plugins/dsq-quant-bridge/，cordis.patch.yml insert 挂载，重启自动生效。
 */
module.exports = {
  inject: ['webServer'],
  apply(ctx) {
    const sessionQuery = ctx.get('sessionQuery')
    const subprocess = ctx.get('subprocess')
    const sessions = ctx.get('sessions')
    const agents = ctx.get('agents')          // ★进程内投递 chat2 用（对齐 v1.0.8 桥接）
    const fsSvc = ctx.get('fs')
    const llm = ctx.get('llm')
    const agentDefaultModel = ctx.get('agentDefaultModel')
    const NODE = process.env.NODE_PATH || 'node'
    const DSH_BIN = 'npx'
    const DSH_HOME = process.env.DSH_HOME || (require('os').homedir() + '/.dsh')
    const ARCH_FILE = DSH_HOME + '/quantapi_archived.json'
    const NIU_FILE = DSH_HOME + '/quantapi_niu_chat.json'
    const SKILL_DIR = require('path').join(__dirname, '..', '..', '..', 'assets', 'skills') + '/'
    const PERSONAS = {
      linyuan: { name: '林园', dir: 'niu-san-linyuan' },
      chenxiaoqun: { name: '陈小群', dir: 'niu-san-chenxiaoqun' },
      zhangmengzhu: { name: '章盟主', dir: 'niu-san-zhangmengzhu' },
      zhaolaoge: { name: '赵老哥', dir: 'niu-san-zhaolaoge' },
      chaoguyangjia: { name: '炒股养家', dir: 'niu-san-chaoguyangjia' },
      fengliu: { name: '冯柳', dir: 'niu-san-fengliu' },
    }
    const CORS = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Access-Control-Request-Private-Network',
      'Access-Control-Allow-Private-Network': 'true',
    }
    let cacheSessions = null
    let cacheAt = 0
    let pendingSessions = null
    let archivedIds = {}
    let niuChats = {}
    let niuBusy = {}
    const CORE_WORD = '主系统'

    async function loadStore() {
      try {
        if (!fsSvc) return
        const t = await fsSvc.resolve(ARCH_FILE)
        const txt = await fsSvc.readText(t)
        const d = JSON.parse(txt)
        archivedIds = {}
        if (d && Array.isArray(d.ids)) d.ids.forEach(function (x) { archivedIds[String(x)] = true })
      } catch (e) {}
      try {
        if (!fsSvc) return
        const t = await fsSvc.resolve(NIU_FILE)
        const txt = await fsSvc.readText(t)
        const d = JSON.parse(txt)
        if (d && typeof d === 'object') niuChats = d
      } catch (e) {}
    }
    async function saveStore() {
      try {
        if (!fsSvc) return
        const t = await fsSvc.resolve(ARCH_FILE)
        await fsSvc.writeText(t, JSON.stringify({ ids: Object.keys(archivedIds) }))
      } catch (e) {}
    }
    async function saveNiu() {
      try {
        if (!fsSvc) return
        const t = await fsSvc.resolve(NIU_FILE)
        await fsSvc.writeText(t, JSON.stringify(niuChats))
      } catch (e) {}
    }
    function markArchived(id) {
      archivedIds[String(id)] = true
      saveStore()
    }

    // ★2026-08-16 牛散聊天：persona 档案人格 + 全量量化快照 → LLM 生成 → 历史文件
    async function niuGenerate(persona, text, snapshot) {
      if (niuBusy[persona]) return
      niuBusy[persona] = true
      try {
        const meta = PERSONAS[persona]
        if (!meta) return
        const hist = (niuChats[persona] || []).slice(-6)
        hist.push({ role: 'user', text: String(text).slice(0, 2000) })
        niuChats[persona] = hist
        await saveNiu()
        let profile = ''
        try {
          if (fsSvc) {
            const t = await fsSvc.resolve(SKILL_DIR + meta.dir + '/SKILL.md')
            profile = await fsSvc.readText(t)
          }
        } catch (e) {}
        profile = String(profile || '').slice(0, 3000)
        let provider = 'deepseek'
        let model = 'deepseek-chat'
        try {
          if (agentDefaultModel && agentDefaultModel.currentSelection) {
            const sel = await agentDefaultModel.currentSelection()
            if (sel && sel.provider) provider = sel.provider
            /* model ?? deepseek-chat(????,??????) */
          }
        } catch (e) {}
        const sys = [
          '你是「' + meta.name + '」——A股知名牛散/投资人物格模拟，基于以下真实公开档案回答选股问题。',
          '【人格档案】\n' + profile,
          '【当前量化全量数据快照（主系统最新）】\n' + (String(snapshot || '') || '（暂无快照）'),
          '【规则】1) 用第一人称、符合人格的语气简短作答（≤300字）；2) 选股建议必须基于快照中的股票；',
          '3) 若做出明确选股决策，在回复末尾附一行 JSON：{"niu_decisions":[{"code":"600000","action":"buy","reason":"一句话"}]}，最多 5 只；',
          '4) 无明确机会时不要编造决策；5) 输出不构成投资建议。',
        ].join('\n')
        const messages = hist.slice(0, -1).map(function (m) {
          return { id: 'niu-h' + Math.random().toString(36).slice(2, 10), role: m.role === 'user' ? 'user' : 'assistant', content: [{ type: 'text', text: m.text }], source: m.role === 'user' ? { kind: 'user' } : { kind: 'model', provider: provider, model: model } }
        })
        messages.push({ id: 'niu-u' + Math.random().toString(36).slice(2, 10), role: 'user', content: [{ type: 'text', text: String(text).slice(0, 2000) }], source: { kind: 'user' } })
        let out = ''
        if (!llm || !llm.stream) throw new Error('llm 服务不可用')
        for await (const c of llm.stream({ provider: provider, model: model, messages: messages, system: sys, temperature: 0.7, maxTokens: 1000 })) {
          if (c && c.type === 'text-delta' && c.text) out += c.text; else if (c && c.type === 'reasoning-delta' && c.text) out += c.text
        }
        out = String(out || '').trim()
        if (!out) out = '（模型未返回内容，请稍后重试）'
        const list = (niuChats[persona] || [])
        list.push({ role: 'assistant', text: out })
        niuChats[persona] = list.slice(-60)
        await saveNiu()
      } finally {
        niuBusy[persona] = false
      }
    }

    function sendJson(res, code, obj) {
      const body = JSON.stringify(obj)
      res.writeHead(code, Object.assign({ 'Content-Type': 'application/json; charset=utf-8' }, CORS))
      res.end(body)
    }
    function readBody(req) {
      return new Promise(function (resolve, reject) {
        let body = ''
        req.setEncoding('utf-8')
        req.on('data', function (c) { body += c })
        req.on('end', function () {
          try { resolve(JSON.parse(body || '{}')) } catch (e) { reject(e) }
        })
        req.on('error', reject)
      })
    }
    function msgText(m) {
      const c = m && m.content
      if (!c) return ''
      if (typeof c === 'string') return c
      if (Array.isArray(c)) {
        return c.map(function (b) { return (b && b.type === 'text' && b.text) ? b.text : '' }).join('')
      }
      return ''
    }
    // ★修复：会话事件有两种形态——消息本体，或 {type:'user/message', data:{message:{...}}}。
    //   原实现只认前者，导致 /quantapi/recent 对真实会话也返回空消息列表（聊天面板永远空白）。
    function unwrapMessage(e) {
      if (!e) return null
      const d = e.data || e
      if (d && d.message && d.message.role) return d.message
      if (d && d.role) return d
      return null
    }
    function roleOf(e) {
      if (!e) return null
      const m = unwrapMessage(e)
      if (m && (m.role === 'user' || m.role === 'assistant')) return m.role
      if (e.type === 'user/message') return 'user'
      if (e.type === 'assistant/message') return 'assistant'
      return null
    }
    function textOf(e) {
      const m = unwrapMessage(e)
      return m ? msgText(m) : ''
    }
    function isInjected(text) {
      const t = String(text || '')
      if (t.indexOf('<system-reminder') === 0) return true
      if (t.indexOf('Current runtime context') === 0) return true
      if (t.indexOf('【任务】你是「') === 0) return true
      return false
    }
    function queryParam(req, key) {
      const raw = String(req.url || '')
      const q = raw.split('?')[1] || ''
      const parts = q.split('&')
      for (let i = 0; i < parts.length; i++) {
        const kv = parts[i].split('=')
        if (kv.length === 2 && decodeURIComponent(kv[0]) === key) return decodeURIComponent(kv[1])
      }
      return null
    }
    function isLive(id) {
      const liveIds = sessions ? sessions.list().map(function (s) { return s.id }) : []
      return liveIds.indexOf(id) >= 0
    }
    // ★chat2 进程内投递需要拿到"活着的" Agent 句柄（与 v1.0.8 桥接同源）
    function liveRootIds() {
      if (!agents) return []
      try { return (agents.roots ? agents.roots() : []).map(function (a) { return String(a.id) }) } catch (e) { return [] }
    }
    function defaultAgentId() {
      const ids = liveRootIds()
      return ids.length ? ids[0] : null
    }
    function resolveAgent(id) {
      if (!agents) return null
      const want = String(id == null ? '' : id)
      if (!want) return null
      try {
        if (typeof agents.get === 'function') {
          const a = agents.get(want)
          if (a) return a
        }
        const roots = agents.roots ? agents.roots() : []
        for (let i = 0; i < roots.length; i++) {
          if (String(roots[i].id) === want) return roots[i]
        }
      } catch (e) {}
      return null
    }
    function isCoreLocked(title) {
      return String(title || '').indexOf(CORE_WORD) >= 0
    }
    function isLocked(id, title) {
      return isCoreLocked(title)
    }
    // ★面板独立：只写本面板隐藏名单，绝不 archiveSession（GUI/workspace 完全不受影响）
    async function hideOne(sid) {
      markArchived(String(sid))
      return true
    }
    function buildSessions() {
      if (pendingSessions) return pendingSessions
      pendingSessions = (async function () {
        const records = sessionQuery ? await sessionQuery.listSessions() : []
        const sorted = records.slice().sort(function (a, b) { return (b.header.createdAt || 0) - (a.header.createdAt || 0) })
        const top = sorted.slice(0, 60)
        const ids = top.map(function (r) { return r.header.id })
        const titles = {}
        try {
          if (sessionQuery && sessionQuery.readTitleSnapshots) {
            const obs = await sessionQuery.readTitleSnapshots(ids)
            for (let i = 0; i < obs.length; i++) {
              const o = obs[i]
              if (o && o.status === 'fulfilled' && o.value && o.value.title) {
                titles[o.sessionId] = typeof o.value.title === 'string' ? o.value.title : (o.value.title.title || '')
              }
            }
          }
        } catch (e) {}
        const liveIds = sessions ? sessions.list().map(function (s) { return s.id }) : []
        const out = []
        for (let i = 0; i < top.length; i++) {
          const id = top[i].header.id
          if (archivedIds[String(id)]) continue
          const title = String(titles[id] || '')
          out.push({
            id: id,
            title: title || ('会话 ' + String(id).slice(0, 8)),
            preview: title.slice(0, 80),
            current: liveIds.indexOf(id) >= 0,
            locked: isLocked(id, title),
            coreLocked: isCoreLocked(title),
          })
        }
        return { sessions: out }
      })().finally(function () { pendingSessions = null })
      return pendingSessions
    }

    ctx.effect(function () {
      loadStore()
      return ctx.webServer.register({
        kind: 'prefix',
        path: '/quantapi',
        handler: async function (req, res) {
          const pathname = String(req.url || '').split('?')[0]
          try {
            if (req.method === 'OPTIONS') {
              res.writeHead(204, CORS)
              return res.end()
            }
            if (req.method === 'GET' && pathname === '/quantapi/sessions') {
              if (cacheSessions && Date.now() - cacheAt < 5000) return sendJson(res, 200, cacheSessions)
              const data = await buildSessions()
              cacheSessions = data
              cacheAt = Date.now()
              return sendJson(res, 200, data)
            }
            if (req.method === 'GET' && pathname === '/quantapi/recent') {
              const sid = queryParam(req, 'sessionId')
              const limit = parseInt(queryParam(req, 'limit') || '30', 10)
              if (!sid) return sendJson(res, 400, { error: 'need sessionId' })
              const sf = sessionQuery ? await sessionQuery.readSurface(sid) : null
              const evs = (sf && Array.isArray(sf.events)) ? sf.events : []
              const msgs = []
              for (let i = 0; i < evs.length; i++) {
                const role = roleOf(evs[i])
                const text = textOf(evs[i])
                if (role && text && !isInjected(text)) msgs.push({ role: role, text: text })
              }
              return sendJson(res, 200, { messages: msgs.slice(-limit) })
            }
            if (req.method === 'GET' && pathname === '/quantapi/niu/sessions') {
              const list = Object.keys(PERSONAS).map(function (id) {
                const m = PERSONAS[id]
                const hist = (niuChats[id] || [])
                const last = hist[hist.length - 1]
                return {
                  id: id,
                  name: m.name,
                  tag: '',
                  preview: last ? String(last.text).slice(0, 60) : '未开聊 —— 首次发言自动建档',
                }
              })
              return sendJson(res, 200, { personas: list })
            }
            if (req.method === 'GET' && pathname === '/quantapi/niu/recent') {
              const persona = queryParam(req, 'persona')
              if (!persona) return sendJson(res, 400, { error: 'need persona' })
              const msgs = (niuChats[persona] || []).slice(-30)
              return sendJson(res, 200, { messages: msgs, busy: !!niuBusy[persona] })
            }
            if (req.method === 'POST' && pathname === '/quantapi/niu/chat') {
              const body = await readBody(req)
              const persona = String(body.persona || '')
              const text = String(body.text || '').trim()
              const snapshot = String(body.snapshot || '')
              if (!PERSONAS[persona]) return sendJson(res, 400, { ok: false, error: '未知 persona：' + persona })
              if (!text) return sendJson(res, 400, { ok: false, error: 'need text' })
              niuGenerate(persona, text, snapshot).catch(function () {})
              return sendJson(res, 200, { ok: true, accepted: true })
            }
            if (req.method === 'POST' && pathname === '/quantapi/restore-all') {
              archivedIds = {}
              await saveStore()
              cacheSessions = null
              return sendJson(res, 200, { ok: true, restored: true })
            }
            if (req.method === 'POST' && pathname === '/quantapi/chat2') {
              const body = await readBody(req)
              const text = String(body.text || '').trim()
              const sid = body.sessionId
              if (!text) return sendJson(res, 400, { ok: false, error: 'need text' })
              if (!sid) return sendJson(res, 400, { ok: false, error: 'need sessionId' })
              // ★修复：原实现 subprocess.spawn 的 argv 是 [NODE, DSH_BIN, '-y', '@deepseek-ai/dsh', ...]，
              //   即 "node npx -y ..."（node 打不开名为 npx 的脚本），且 cwd 指向没有 node_modules 的 DSH_HOME，
              //   必然静默失败却仍返回 accepted:true（前端只会看到"已送达·等回复…"然后超时）。
              //   改为与 v1.0.8 桥接一致的进程内投递：agent.followup(UserMessage)。
              const agent = resolveAgent(sid)
              if (!agent) {
                return sendJson(res, 409, {
                  ok: false,
                  error: '该会话当前不在运行（agent 未激活）—— 请先在 http://127.0.0.1:3080 打开它，再回到本页发送',
                  sessionId: String(sid),
                  rootIds: liveRootIds(),
                })
              }
              // UserMessage 形状对齐 @deepseek-ai/dsh-llm 的 createUserMessage：需带 id/role/content/source
              const message = {
                id: require('crypto').randomUUID(),
                role: 'user',
                content: [{ type: 'text', text: text }],
                source: { kind: 'user' },
              }
              try {
                if (typeof agent.followup === 'function') agent.followup(message)
                else if (typeof agent.steer === 'function') agent.steer(message)
                else return sendJson(res, 500, { ok: false, error: 'agent 无 followup/steer' })
              } catch (e) {
                return sendJson(res, 500, { ok: false, error: '投递失败: ' + String((e && e.message) || e) })
              }
              return sendJson(res, 200, { ok: true, accepted: true, sessionId: String(agent.id) })
            }
            if (req.method === 'POST' && pathname === '/quantapi/delete') {
              const body = await readBody(req)
              const sid = body.sessionId
              if (!sid) return sendJson(res, 400, { ok: false, error: 'need sessionId' })
              let title = ''
              try {
                const t = sessionQuery ? await sessionQuery.readTitle(String(sid)) : null
                if (t && typeof t === 'object' && t.title) title = String(t.title)
              } catch (e) {}
              if (isLocked(String(sid), title)) return sendJson(res, 403, { ok: false, error: '主系统核心会话不可删除' })
              await hideOne(sid)
              cacheSessions = null
              return sendJson(res, 200, { ok: true, archived: true, panelOnly: true })
            }
            if (req.method === 'POST' && pathname === '/quantapi/delete-batch') {
              const body = await readBody(req)
              const ids = Array.isArray(body.sessionIds) ? body.sessionIds : []
              if (!ids.length) return sendJson(res, 400, { ok: false, error: 'need sessionIds' })
              let deleted = 0
              const skipped = []
              for (let i = 0; i < ids.length; i++) {
                const sid = String(ids[i])
                let title = ''
                try {
                  const t = sessionQuery ? await sessionQuery.readTitle(sid) : null
                  if (t && typeof t === 'object' && t.title) title = String(t.title)
                } catch (e) {}
                if (isLocked(sid, title)) { skipped.push({ id: sid, title: title, reason: 'locked' }); continue }
                await hideOne(sid)
                deleted++
              }
              cacheSessions = null
              return sendJson(res, 200, { ok: true, deleted: deleted, skipped: skipped, panelOnly: true })
            }
            if (req.method === 'POST' && pathname === '/quantapi/clear-all') {
              const data = await buildSessions()
              const list = (data && data.sessions) || []
              let deleted = 0
              let lockedCount = 0
              for (let i = 0; i < list.length; i++) {
                const s = list[i]
                if (s.locked) { lockedCount++; continue }
                await hideOne(s.id)
                deleted++
              }
              cacheSessions = null
              return sendJson(res, 200, { ok: true, deleted: deleted, locked: lockedCount, panelOnly: true })
            }
            if (req.method === 'POST' && pathname === '/quantapi/niu/chat') {
              const body = await readBody(req)
              const persona = String(body.persona || '')
              const text = String(body.text || '').trim()
              const snapshot = String(body.snapshot || '')
              if (!PERSONAS[persona]) return sendJson(res, 400, { ok: false, error: '未知 persona：' + persona })
              if (!text) return sendJson(res, 400, { ok: false, error: 'need text' })
              niuGenerate(persona, text, snapshot).catch(function () {})
              return sendJson(res, 200, { ok: true, accepted: true })
            }
            return sendJson(res, 404, { error: 'quant route not found' })
          } catch (e) {
            return sendJson(res, 500, { error: String((e && e.message) || e) })
          }
        },
      })
    })
  },
}
