import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const HistoryView = {
  setup() {
    const items = ref([]); const marks = ref([]); const err = ref("");
    async function reload() {
      err.value = "";
      try {
        const [h, c] = await Promise.all([
          api("/api/history?limit=100"),
          api("/api/locks/contention?limit=40"),
        ]);
        items.value = h.items || [];
        marks.value = c.items || [];
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { items, marks, err, reload };
  },
  template: `
  <div class="grid">
    <div class="panel span-12">
      <h2>变更历史</h2>
      <div class="err" v-if="err">{{ err }}</div>
      <table>
        <thead><tr><th>时间动作</th><th>开关</th><th>环境</th><th>详情</th><th>操作者</th></tr></thead>
        <tbody>
          <tr v-for="it in items" :key="it.id">
            <td>{{ it.action }}</td><td>{{ it.flag_key || '-' }}</td><td>{{ it.env_code || '-' }}</td>
            <td>{{ it.detail }}</td><td>{{ it.actor }}</td>
          </tr>
        </tbody>
      </table>
      <button class="btn secondary" style="margin-top:0.7rem" @click="reload">刷新</button>
    </div>
    <div class="panel span-12">
      <h2>争用痕 · 同草稿口升正互斥（败方只留此痕）</h2>
      <p class="hint">两路并发升正同一草稿口时，败方不写正式/历史/审计成功账，仅在此留一段 contention_marks。</p>
      <table>
        <thead><tr><th>ID</th><th>互斥键</th><th>败方</th><th>胜方</th><th>时间戳</th></tr></thead>
        <tbody>
          <tr v-for="m in marks" :key="m.id" class="row-conflict">
            <td>{{ m.id }}</td><td>{{ m.lock_key }}</td><td>{{ m.loser }}</td><td>{{ m.winner }}</td><td>{{ m.created_at }}</td>
          </tr>
          <tr v-if="!marks.length"><td colspan="5" class="muted">暂无争用</td></tr>
        </tbody>
      </table>
    </div>
  </div>`
};
