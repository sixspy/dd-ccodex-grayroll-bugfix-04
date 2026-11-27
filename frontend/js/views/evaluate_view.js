import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const EvaluateView = {
  setup() {
    const form = ref({ flag_key: "checkout.new_pay", env_code: "prod", subject_key: "u-1001" });
    const result = ref(null); const board = ref(null); const err = ref("");
    async function reloadBoard() { board.value = await api("/api/evaluate/board"); }
    async function run() {
      err.value = "";
      try { result.value = await api("/api/evaluate", { method: "POST", body: JSON.stringify(form.value) }); }
      catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reloadBoard);
    return { form, result, board, err, run, reloadBoard };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>在线求值</h2>
      <div class="field"><label>开关键</label><input v-model="form.flag_key" /></div>
      <div class="field"><label>环境</label>
        <select v-model="form.env_code"><option>dev</option><option>staging</option><option>prod</option></select>
      </div>
      <div class="field"><label>主体键</label><input v-model="form.subject_key" /></div>
      <button class="btn" @click="run">求值</button>
      <div class="err" v-if="err">{{ err }}</div>
      <div class="err" v-if="result && result.dual_official" style="background:#7f1d1d;color:#fff">
        账实冲突：该格存在 {{ result.official_count }} 个正式版本（v{{ (result.official_versions||[]).join(' / v') }}），
        求值已确定性取最高 canonical v{{ result.version }}，但禁止假绿，请核销重复正式。
      </div>
      <p class="hint" v-if="result && !result.dual_official && result.official_count===1">
        正式版本唯一且一致：v{{ result.version }}。
      </p>
      <pre v-if="result" style="margin-top:0.8rem">{{ result }}</pre>
    </div>
    <div class="panel span-7">
      <h2>正式规则板</h2>
      <div class="err" v-if="board && board.official_conflict_count" style="background:#7f1d1d;color:#fff">
        有 {{ board.official_conflict_count }} 个格子存在双正式版本：
        <span v-for="c in board.conflicts" :key="c.flag_key+c.env_code">
          {{ c.flag_key }}@{{ c.env_code }}(×{{ c.official_count }})&nbsp;
        </span>
      </div>
      <table v-if="board">
        <thead><tr><th>开关</th><th>环境</th><th>类型</th><th>%</th><th>版本</th><th>账实</th></tr></thead>
        <tbody>
          <tr v-for="(r,i) in board.rules" :key="i" :class="{ 'row-conflict': r.official_count>1 }">
            <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>{{ r.kind }}</td>
            <td>{{ r.percent }}</td><td>v{{ r.version }}</td>
            <td>
              <span class="badge ok" v-if="r.official_count<=1">唯一</span>
              <span class="badge bad" v-else>双正式 ×{{ r.official_count }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      <button class="btn secondary" style="margin-top:0.7rem" @click="reloadBoard">刷新</button>
    </div>
  </div>`
};
