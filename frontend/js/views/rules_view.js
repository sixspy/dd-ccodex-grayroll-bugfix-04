import { api } from "../api.js";
const { ref, onMounted } = Vue;

const SHADOW_STATUS_META = {
  open: { label: "开放中 · 可升正", cls: "badge warn" },
  promoted: { label: "已升正 · 草稿口已关闭（已消耗）", cls: "badge ok" },
  discarded: { label: "已废弃 · 口已关闭", cls: "badge muted" },
};

export const RulesView = {
  setup() {
    const official = ref([]); const shadows = ref([]);
    const form = ref({ flag_id: 3, env_id: 2, kind: "percent", percent: 10, cohort_id: null });
    const err = ref(""); const msg = ref("");
    const promotingId = ref(null);
    async function reload() {
      const res = await api("/api/rules");
      official.value = res.official || []; shadows.value = res.shadows || [];
    }
    async function shadowBind() {
      err.value = ""; msg.value = "";
      try {
        const body = { ...form.value, cohort_id: form.value.cohort_id || null };
        await api("/api/rules/shadow-bind", { method: "POST", body: JSON.stringify(body) });
        msg.value = "影子绑定已写入（草稿口开放，升正前正式零改）"; await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function promote(id) {
      if (promotingId.value !== null) return; // 前端先挡连点；最终互斥以后端锁+核销为准
      err.value = ""; msg.value = ""; promotingId.value = id;
      try {
        const res = await api(`/api/rules/shadow/${id}/promote`, { method: "POST", body: "{}" });
        msg.value = `升正成功 v${res.version}：正式规则已落库，草稿口同事务关闭/已消耗。`;
        await reload();
      } catch (e) {
        // 409=争用败方/草稿口已消耗：刷新后该行应是 promoted，不再给升正按钮。
        err.value = e.message || String(e);
        await reload();
      } finally { promotingId.value = null; }
    }
    function statusMeta(s) { return SHADOW_STATUS_META[s] || { label: s, cls: "badge muted" }; }
    onMounted(reload);
    return { official, shadows, form, err, msg, promotingId, shadowBind, promote, reload, statusMeta };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>影子绑定（乙）</h2>
      <p class="hint">只写影子草案，正式规则升正前零改。升正按甲口径：落正式与关闭草稿口同事务。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>类型</label>
        <select v-model="form.kind"><option>percent</option><option>cohort</option><option>percent_and_cohort</option></select>
      </div>
      <div class="field"><label>比例</label><input type="number" v-model.number="form.percent" /></div>
      <button class="btn" @click="shadowBind">写入影子</button>
      <p class="muted" v-if="msg" style="color:#15803d">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>正式规则</h2>
      <table><thead><tr><th>开关</th><th>环境</th><th>类型</th><th>%</th><th>版本</th></tr></thead>
      <tbody>
        <tr v-for="r in official" :key="r.id">
          <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>{{ r.kind }}</td><td>{{ r.percent }}</td><td>v{{ r.version }}</td>
        </tr>
        <tr v-if="!official.length"><td colspan="5" class="muted">暂无正式规则</td></tr>
      </tbody></table>
      <h2 style="margin-top:1rem">影子草案 / 草稿口</h2>
      <table><thead><tr><th>ID</th><th>开关</th><th>环境</th><th>草稿口状态</th><th></th></tr></thead>
      <tbody>
        <tr v-for="s in shadows" :key="s.id" :class="{ 'row-consumed': s.status!=='open' }">
          <td>{{ s.id }}</td><td>{{ s.flag_id }}</td><td>{{ s.env_id }}</td>
          <td><span :class="statusMeta(s.status).cls">{{ statusMeta(s.status).label }}</span></td>
          <td>
            <button class="btn secondary" v-if="s.status==='open'"
                    :disabled="promotingId===s.id" @click="promote(s.id)">
              {{ promotingId===s.id ? '升正中…' : '升正并关口' }}
            </button>
            <span class="muted" v-else>—</span>
          </td>
        </tr>
      </tbody></table>
      <p class="hint">同一草稿口并发升正由后端互斥：仅一路成功，败方 409 且只留争用痕；二次升正不再落正式。</p>
    </div>
  </div>`
};
