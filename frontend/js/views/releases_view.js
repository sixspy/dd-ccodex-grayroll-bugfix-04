import { api } from "../api.js";
const { ref, onMounted } = Vue;

const SHADOW_STATUS_META = {
  open: { label: "开放中 · 可升正", cls: "badge warn" },
  promoted: { label: "已升正 · 草稿口已关闭（已消耗）", cls: "badge ok" },
  discarded: { label: "已废弃 · 口已关闭", cls: "badge muted" },
};

export const ReleasesView = {
  setup() {
    const official = ref([]); const shadows = ref([]);
    const form = ref({
      flag_id: 3, env_id: 1, kind: "percent", percent: 30, cohort_id: null,
      summary: "开发环境三成灰度试跑", use_shadow: false,
    });
    const err = ref(""); const msg = ref("");
    const promotingId = ref(null);
    async function reload() {
      const res = await api("/api/releases");
      official.value = res.official || []; shadows.value = res.shadows || [];
    }
    async function publish() {
      err.value = ""; msg.value = "";
      try {
        const body = { ...form.value, cohort_id: form.value.cohort_id || null };
        const res = await api("/api/releases/publish", { method: "POST", body: JSON.stringify(body) });
        msg.value = res.mode === "shadow"
          ? `影子发布草案 #${res.id} 已写入，正式区零改，待原子升正。`
          : `已正式发布 v${res.version}（同事务落正式配置）。`;
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function promote(id) {
      if (promotingId.value !== null) return;
      err.value = ""; msg.value = ""; promotingId.value = id;
      try {
        const res = await api(`/api/releases/shadow/${id}/promote`, { method: "POST", body: "{}" });
        msg.value = `升正成功 v${res.version}：正式发布已落库，草稿口同事务关闭/已消耗。`;
        await reload();
      } catch (e) {
        err.value = e.message || String(e);
        await reload();
      } finally { promotingId.value = null; }
    }
    function statusMeta(s) { return SHADOW_STATUS_META[s] || { label: s, cls: "badge muted" }; }
    onMounted(reload);
    return { official, shadows, form, err, msg, promotingId, publish, promote, reload, statusMeta };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>发布生效</h2>
      <p class="hint">默认甲路径：同事务写正式配置并升版本。勾选影子则走乙路径，升正时原子核销草稿口。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>类型</label>
        <select v-model="form.kind"><option>percent</option><option>cohort</option><option>percent_and_cohort</option></select>
      </div>
      <div class="field"><label>比例</label><input type="number" v-model.number="form.percent" /></div>
      <div class="field"><label>说明</label><textarea v-model="form.summary" rows="2"></textarea></div>
      <div class="field"><label><input type="checkbox" v-model="form.use_shadow" /> 使用影子草案</label></div>
      <button class="btn" @click="publish">提交发布</button>
      <p class="muted" v-if="msg" style="color:#15803d">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>正式发布</h2>
      <table><thead><tr><th>开关</th><th>环境</th><th>版本</th><th>说明</th></tr></thead>
      <tbody>
        <tr v-for="r in official" :key="r.id">
          <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>v{{ r.version }}</td><td>{{ r.summary }}</td>
        </tr>
        <tr v-if="!official.length"><td colspan="4" class="muted">暂无正式发布</td></tr>
      </tbody></table>
      <h2 style="margin-top:1rem">影子发布 / 草稿口</h2>
      <table><thead><tr><th>ID</th><th>开关</th><th>草稿口状态</th><th></th></tr></thead>
      <tbody>
        <tr v-for="s in shadows" :key="'s'+s.id" :class="{ 'row-consumed': s.status!=='open' }">
          <td>{{ s.id }}</td><td>{{ s.flag_id }}</td>
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
      <p class="hint">两路并发升正同一草稿口：仅一路 200，另一路 409 且只留争用痕，不会产生第二条正式发布。</p>
    </div>
  </div>`
};
