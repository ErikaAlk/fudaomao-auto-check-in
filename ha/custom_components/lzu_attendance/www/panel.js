const tokenResponse = await fetch('/lzu_attendance_static/tokens.json');
if (!tokenResponse.ok) throw new Error('界面参数加载失败');
const tokens = await tokenResponse.json();
const px = value => String(value).replace(/dp|sp/g, 'px');
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const busyStates = ['queued', 'running', 'waiting_sms', 'sms_received'];
const style = `
:host{display:block;height:100%;overflow:auto;background:var(--bg);color:var(--text);font-family:var(--paper-font-body1_-_font-family,inherit);font-size:${px(tokens.type.bodyXL.size)}}
*{box-sizing:border-box}main{max-width:960px;margin:auto;padding:24px 24px 64px}header,.row,.actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}header{justify-content:space-between;margin-bottom:24px}h1{font-size:${px(tokens.type.displayS.size)};font-weight:600;margin:0}h2{font-size:${px(tokens.type.headlineM.size)};margin:0}h3{font-size:${px(tokens.type.headlineXS.size)};margin:0}p{line-height:1.6;overflow-wrap:anywhere}small,.meta{color:var(--muted);font-size:${px(tokens.type.bodyS.size)}}
section,.job{padding:24px;background:var(--card);border-radius:${px(tokens.radius.xl)};margin-bottom:16px}.job{background:var(--fill);margin:16px 0 0}.user-head{justify-content:space-between}.job-head{justify-content:space-between;align-items:flex-start}.job-name{flex:1;min-width:140px;overflow-wrap:anywhere}.meta{margin:8px 0 16px}.empty{padding:40px 0;color:var(--muted)}
button,input,textarea,select{font:inherit;color:inherit}button{min-height:${px(tokens.button.largeHeight)};padding:8px 20px;border:0;border-radius:${px(tokens.radius.full)};background:var(--button);cursor:pointer}button.primary{background:var(--primary);color:var(--on-primary)}button:disabled{opacity:.5;cursor:wait}button:hover{filter:brightness(.96)}button:active{transform:scale(.97)}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--primary);outline-offset:3px}input:not([type=checkbox]),textarea{display:block;width:100%;min-width:0;min-height:${px(tokens.button.largeHeight)};border:0;border-radius:${px(tokens.radius.xl)};padding:12px 16px;background:var(--fill)}textarea{resize:vertical;min-height:92px}label{display:block;margin:20px 0 8px}input[type=checkbox]{width:22px;height:22px;accent-color:var(--primary)}.check{display:flex;align-items:center;gap:8px;margin:0;min-height:44px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}.weekdays{display:flex;gap:8px;flex-wrap:wrap}.weekdays label{display:flex;align-items:center;gap:4px;margin:8px 0}.status{min-height:24px;white-space:pre-wrap}.error{color:var(--error)}.status:empty{display:none}
dialog{width:min(540px,calc(100vw - 24px));max-height:calc(100dvh - 32px);padding:24px;border:0;border-radius:${px(tokens.radius.xxxxl)};background:var(--surface);color:var(--text);overflow:auto}dialog::backdrop{background:#0008}dialog .actions{justify-content:flex-end;margin-top:24px}dialog h2{padding-right:24px}.step{font-size:${px(tokens.type.bodyS.size)};color:var(--muted);margin:8px 0 24px}.full{grid-column:1/-1}.toolbar{margin-top:16px}.menu{padding:8px 14px}.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
@media(max-width:540px){main{padding:16px 12px 48px}section,.job{padding:16px}.grid{grid-template-columns:1fr}header{gap:16px}.actions button{flex:1}h1{font-size:${px(tokens.type.displayXS.size)}}
@media(prefers-reduced-motion:reduce){button:active{transform:none}}@media(forced-colors:active){button,input,textarea,dialog{border:1px solid CanvasText}}
`;

class AttendancePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:'open'});
    this.data = null;
    this.activeOperation = null;
    this.shadowRoot.innerHTML = `<style>${style}</style><main><header><div class="row"><button class="menu" aria-label="打开侧边栏">☰</button><h1>签到</h1></div><button id="new-user" class="primary">新建用户</button></header><p id="global-status" class="status" role="status" aria-live="polite"></p><div id="users"><p>正在读取用户</p></div></main><dialog aria-labelledby="dialog-title"><h2 id="dialog-title"></h2><p class="step" id="step"></p><div id="dialog-body"></div><p class="status" id="dialog-status" role="status" aria-live="polite"></p><div class="actions"><button id="close">关闭</button><button id="next" class="primary"></button></div></dialog>`;
    this.$('.menu').onclick = () => this.dispatchEvent(new Event('hass-toggle-menu', {bubbles:true,composed:true}));
    this.$('#new-user').onclick = () => this.openNew();
    this.$('#close').onclick = () => this.$('dialog').close();
    this.$('dialog').addEventListener('close', () => {
      this.activeOperation = null;
      const opener = this.dialogOpener ? this.$(`[data-focus="${CSS.escape(this.dialogOpener)}"]`) : null;
      (opener || this.$('#new-user')).focus();
    });
  }

  $(selector) { return this.shadowRoot.querySelector(selector); }

  set hass(value) {
    this._hass = value;
    const theme = value.themes.darkMode ? 'dark' : 'light';
    const color = (key, name) => {
      const value = tokens.color[key][theme];
      this.style.setProperty(name, '#' + value.slice(3) + value.slice(1,3));
    };
    for (const [key,name] of Object.entries({bgGrouped:'--bg',card:'--card',label1:'--text',label2:'--muted',primary:'--primary',onPrimary:'--on-primary',fill8:'--fill',surface:'--surface',error:'--error'})) color(key,name);
    const button = tokens.button.secondaryBg[theme];
    this.style.setProperty('--button','#' + button.slice(3) + button.slice(1,3));
    if (!this.started) { this.started = true; this.execute(() => this.refresh()); }
  }

  connectedCallback() {
    // HA 可能在自定义元素注册前赋值，恢复属性的 setter。
    if (Object.hasOwn(this, 'hass')) {
      const value = this.hass;
      delete this.hass;
      this.hass = value;
    }
    this.timer = setInterval(() => {
      if (this._hass && !this.refreshing) this.execute(() => this.refresh());
    }, 2500);
  }

  disconnectedCallback() { clearInterval(this.timer); }

  execute(work) {
    return work().then(() => {}, error => {
      const target = this.$('dialog').open ? this.$('#dialog-status') : this.$('#global-status');
      target.classList.add('error');
      target.textContent = error.body?.detail || error.message || '操作失败，请检查服务连接';
      this.$('#next').disabled = false;
    });
  }

  async call(payload) {
    return this._hass.callApi('POST','lzu_attendance',payload);
  }

  async refresh() {
    this.refreshing = true;
    try {
      this.data = await this._hass.callApi('GET','lzu_attendance');
      this.renderUsers();
      this.renderOperation();
    } finally { this.refreshing = false; }
  }

  renderUsers() {
    const users = this.$('#users');
    const fingerprint = JSON.stringify(this.data);
    if (fingerprint === this.fingerprint) return;
    this.fingerprint = fingerprint;
    const focused = this.shadowRoot.activeElement;
    const focusId = focused?.dataset?.focus;
    users.innerHTML = this.data.users.length ? this.data.users.map(user => {
      const op = this.data.operations.find(op => op.user_id === user.id);
      const busy = op && busyStates.includes(op.state);
      const jobs = this.data.jobs.filter(job => job.user_id === user.id);
      return `<section><div class="row user-head"><div><h2>${escape(user.alias)}</h2><p class="meta">${escape(user.username)} · ${user.ready ? '已验证' : '待验证'}</p></div><div class="row"><button data-action="alias" data-user="${user.id}">修改别名</button><button data-action="login" data-user="${user.id}">${busy ? '查看进度' : '登录验证'}</button><button data-action="add" data-user="${user.id}" ${!user.ready || busy ? 'disabled' : ''}>添加任务</button></div></div>${op ? `<p class="status ${op.state === 'failed' ? 'error' : ''}">${escape(op.message || '正在等待执行')}</p>` : ''}${jobs.map(job => {
        const latest = this.data.operations.find(op => op.job_id === job.id && ['run','trial','check'].includes(op.action));
        const day = this.data.days.find(day => day.job_id === job.id);
        const weekday = job.weekdays.length === 7 ? '每天' : job.weekdays.map(n => '周' + ['一','二','三','四','五','六','日'][n-1]).join('、');
        return `<article class="job"><div class="row job-head"><div class="job-name"><h3>${escape(job.name)}</h3><p class="meta">${escape(weekday)} · ${escape(job.run_at)}–${escape(job.deadline)} · 北京时间${day ? ` · 今日 ${day.attempts} 次` : ''}</p></div><label class="check"><input type="checkbox" data-toggle="${job.id}" aria-label="${escape(job.name)}自动签到" ${job.enabled ? 'checked' : ''} ${busy || !job.saved ? 'disabled' : ''}>自动签到</label></div>${latest ? `<p>${escape(latest.message)}</p>` : ''}<div class="row"><button data-action="settings" data-job="${job.id}" data-focus="settings-${job.id}" ${busy || job.name === '正在识别' ? 'disabled' : ''}>${job.saved ? '任务设置' : '继续配置'}</button><button data-action="check" data-job="${job.id}" ${busy || !job.saved ? 'disabled' : ''}>检查状态</button><button data-action="run" data-job="${job.id}" ${busy || !job.saved ? 'disabled' : ''}>立即签到</button></div></article>`;
      }).join('')}</section>`;
    }).join('') : '<p class="empty">还没有用户</p>';
    users.querySelectorAll('[data-action]').forEach(button => {
      button.dataset.focus = button.dataset.action + '-' + (button.dataset.user || button.dataset.job);
      button.onclick = () => this.execute(async () => {
      const user = this.data.users.find(user => user.id === button.dataset.user);
      const job = this.data.jobs.find(job => job.id === button.dataset.job);
      const action = button.dataset.action;
      if (action === 'add') this.openLink(user);
      if (action === 'settings') this.openSettings(job);
      if (action === 'alias') this.openAlias(user);
      if (action === 'login') {
        const pending = this.data.operations.find(op => op.user_id === user.id && busyStates.includes(op.state));
        if (pending) this.watch(pending.id, user.id, pending.action);
        else this.openLogin(user);
      }
      if (['run','check'].includes(action)) {
        const result = await this.call({action,job_id:job.id});
        this.watch(result.operation_id,job.user_id,action);
      }
      });
    });
    users.querySelectorAll('[data-toggle]').forEach(input => {
      input.dataset.focus = 'toggle-' + input.dataset.toggle;
      input.onchange = () => this.execute(async () => {
      await this.call({action:'toggle',job_id:input.dataset.toggle,enabled:input.checked});
      await this.refresh();
      });
    });
    if (focusId) users.querySelector(`[data-focus="${CSS.escape(focusId)}"]`)?.focus();
  }

  dialog(title, step, body, next, callback) {
    this.$('#dialog-title').textContent = title;
    this.$('#step').textContent = step;
    this.$('#dialog-body').innerHTML = body;
    this.$('#dialog-status').textContent = '';
    this.$('#dialog-status').classList.remove('error');
    this.$('#next').textContent = next;
    this.$('#next').hidden = !next;
    this.$('#next').disabled = false;
    this.$('#next').onclick = () => this.execute(async () => {
      const form = this.$('#dialog-body form');
      if (form && !form.reportValidity()) return;
      this.$('#next').disabled = true;
      try { await callback(); } finally { this.$('#next').disabled = false; }
    });
    if (!this.$('dialog').open) {
      this.dialogOpener = this.shadowRoot.activeElement?.dataset?.focus;
      this.$('dialog').showModal();
    }
    this.$('#dialog-body form')?.addEventListener('submit', event => { event.preventDefault(); this.$('#next').click(); });
    this.$('#dialog-body input')?.focus();
  }

  openNew() {
    this.activeOperation = null;
    this.dialog('新建用户','账号信息',`<form><label for="alias">别名</label><input id="alias" required maxlength="80" autocomplete="off"><label for="username">账号名称</label><input id="username" required maxlength="80" autocomplete="username"><label for="password">密码</label><input id="password" required type="password" maxlength="512" autocomplete="new-password"><button class="sr" type="submit" tabindex="-1">请求验证码</button></form>`,'请求验证码', async () => {
      const result = await this.call({action:'create_user',alias:this.$('#alias').value,username:this.$('#username').value,password:this.$('#password').value});
      this.$('#password').value = '';
      this.watch(result.operation_id,result.user_id,'login');
    });
  }

  openLogin(user) {
    this.dialog(user.alias,'学校登录验证','<form><label for="password">密码</label><input id="password" type="password" autocomplete="current-password"><p class="meta">留空使用已保存的密码。</p></form>','请求验证码',async () => {
      const result = await this.call({action:'login',user_id:user.id,password:this.$('#password').value});
      this.$('#password').value = '';
      this.watch(result.operation_id,user.id,'login');
    });
  }

  openAlias(user) {
    this.dialog('修改别名','',`<form><label for="alias">别名</label><input id="alias" required maxlength="80" value="${escape(user.alias)}"></form>`,'保存',async () => {
      await this.call({action:'alias',user_id:user.id,alias:this.$('#alias').value});
      this.$('dialog').close();
      await this.refresh();
    });
  }

  openLink(user) {
    this.activeOperation = null;
    this.dialog('添加任务',user.alias,`<form><label for="link">近期辅导猫签到链接</label><textarea id="link" required maxlength="4096" autocomplete="off"></textarea></form>`,'检测任务名称',async () => {
      const result = await this.call({action:'detect',user_id:user.id,link:this.$('#link').value.trim()});
      this.$('#link').value = '';
      this.watch(result.operation_id,user.id,'detect',result.job_id);
    });
  }

  openSettings(job) {
    this.activeOperation = null;
    const loc = job.location;
    this.dialog(job.name,'签到设置 · 北京时间',`<form><div class="grid"><div><label for="run-at">执行时间</label><input id="run-at" type="time" required value="${escape(job.run_at)}"></div><div><label for="deadline">截止时间</label><input id="deadline" type="time" required value="${escape(job.deadline)}"></div><div class="full"><label for="start-date">开始日期</label><input id="start-date" type="date" required value="${escape(job.start_date)}"></div></div><label>执行星期</label><div class="weekdays">${['一','二','三','四','五','六','日'].map((day,i) => `<label><input type="checkbox" name="weekday" value="${i+1}" ${job.weekdays.includes(i+1) ? 'checked' : ''}>周${day}</label>`).join('')}</div><label for="address">实际签到地址</label><input id="address" required maxlength="512" value="${escape(loc.address || '')}"><div class="grid"><div><label for="longitude">经度（GCJ-02）</label><input id="longitude" required type="number" step="any" min="-180" max="180" value="${escape(loc.longitude ?? '')}"></div><div><label for="latitude">纬度（GCJ-02）</label><input id="latitude" required type="number" step="any" min="-90" max="90" value="${escape(loc.latitude ?? '')}"></div></div><p class="meta">使用高德或腾讯地图的实际位置。每项任务失败后等待五分钟，每天最多二十次。</p></form>`,job.saved ? '保存设置' : '尝试签到并保存',async () => {
      const weekdays = [...this.shadowRoot.querySelectorAll('[name=weekday]:checked')].map(input => Number(input.value));
      await this.call({action:'configure',job_id:job.id,run_at:this.$('#run-at').value,deadline:this.$('#deadline').value,start_date:this.$('#start-date').value,weekdays,address:this.$('#address').value,longitude:Number(this.$('#longitude').value),latitude:Number(this.$('#latitude').value)});
      if (job.saved) { this.$('dialog').close(); await this.refresh(); }
      else {
        const result = await this.call({action:'trial',job_id:job.id});
        this.watch(result.operation_id,job.user_id,'trial',job.id);
      }
    });
  }

  watch(id,userId,action,jobId) {
    this.activeOperation = {id,userId,action,jobId,shownState:null};
    this.dialog('学校验证','', '<p>正在连接学校</p>','',async () => {});
    this.execute(() => this.refresh());
  }

  renderOperation() {
    if (!this.activeOperation || !this.$('dialog').open) return;
    const active = this.activeOperation;
    const op = this.data.operations.find(op => op.id === active.id);
    if (!op || active.shownState === op.state) return;
    active.shownState = op.state;
    const user = this.data.users.find(user => user.id === active.userId);
    if (op.state === 'waiting_sms') {
      this.dialog('短信验证',user.alias,'<form><label for="code">短信验证码</label><input id="code" required inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{4,8}" minlength="4" maxlength="8"></form>','验证',async () => {
        await this.call({action:'code',operation_id:op.id,code:this.$('#code').value});
        this.$('#code').value = '';
        this.$('#next').hidden = true;
        this.$('#dialog-body').textContent = '正在验证短信';
      });
    } else if (op.state === 'success') {
      this.activeOperation = null;
      if (active.action === 'login') this.openLink(user);
      else if (active.action === 'detect') {
        const job = this.data.jobs.find(job => job.id === op.job_id);
        if (op.result.sign_mode !== '0' || op.result.photo_required) {
          this.dialog(job.name,'任务已识别','<p>此任务要求拍照或其他签到方式，请在学校客户端完成。</p>','',async () => {});
        } else this.openSettings(job);
      } else this.dialog('执行结果','',`<p>${escape(op.message)}</p>`,'',async () => {});
    } else if (op.state === 'failed') {
      this.activeOperation = null;
      const job = this.data.jobs.find(job => job.id === op.job_id);
      const next = active.action === 'login' ? '重新登录' : active.action === 'detect' ? '重新填写链接' : job && !job.saved ? '修改设置' : '';
      this.dialog('执行结果','',`<p class="error">${escape(op.message)}</p>`,next,async () => {
        if (active.action === 'login') this.openLogin(user);
        else if (active.action === 'detect') this.openLink(user);
        else this.openSettings(job);
      });
    } else {
      this.$('#dialog-status').textContent = op.message || '正在等待执行';
    }
  }
}

customElements.define('lzu-attendance-panel', AttendancePanel);
