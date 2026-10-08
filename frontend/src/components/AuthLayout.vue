<template>
  <div class="auth-shell">
    <div class="auth-story">
      <div class="auth-brand"><span class="brand-mark">I<span>·</span></span> InnovationCore AI</div>
      <div class="auth-content">
        <div class="auth-kicker">ENTERPRISE AI PLATFORM</div>
        <h1>让每一次模型创新<br />都有迹可循。</h1>
        <p>模型服务化 · 数据与评测 · Agent 应用 · AI 实训</p>
        <div class="auth-lines"><span>01 / 接入</span><span>02 / 评测</span><span>03 / 应用</span></div>
      </div>
      <div class="auth-bottom">INNOVATIONCORE / AI PLATFORM</div>
    </div>
    <main class="auth-form-wrap"><div class="auth-form"><slot /></div></main>
  </div>
</template>

<style scoped>
.auth-shell { display: grid; grid-template-columns: 54% 46%; min-height: 100vh; background: #fff; }
.auth-story { background: #b9152a; color: white; padding: 43px 61px; display: flex; flex-direction: column; position: relative; overflow: hidden; }
.auth-story::before { content: ''; position: absolute; width: 590px; height: 590px; border: 1px solid #fff3; border-radius: 50%; right: -235px; top: 15%; box-shadow: 0 0 0 100px #ffffff0a, 0 0 0 220px #ffffff08; }
.auth-brand { font-size: 17px; font-weight: 750; display: flex; align-items: center; gap: 12px; z-index: 1; }
.auth-brand .brand-mark { background: white; color: #bd1a2c; box-shadow: none; width: 34px; height: 34px; font-size: 19px; }
.auth-brand .brand-mark span { color: #bd1a2c88; }
.auth-content { margin: auto 0; z-index: 1; }
.auth-kicker { font-size: 11px; letter-spacing: 3px; color: #ffc4ca; font-weight: 750; }
.auth-content h1 { font-size: clamp(38px, 4vw, 62px); line-height: 1.18; letter-spacing: -2.2px; margin: 18px 0 22px; }
.auth-content p { color: #ffd9de; font-size: 16px; }
.auth-lines { display: flex; gap: 26px; border-top: 1px solid #ffffff55; margin-top: 72px; padding-top: 18px; font-size: 11px; letter-spacing: 1.4px; color: #ffe2e5; }
.auth-bottom { z-index: 1; letter-spacing: 2px; font-size: 10px; color: #ffdee2; }
.auth-form-wrap { display: grid; place-items: center; padding: 42px 30px; }
.auth-form { width: min(400px, 100%); }
.auth-form :deep(h2) { font-size: 32px; margin-bottom: 8px; }
.auth-form :deep(.subtitle) { margin-bottom: 30px; }
.auth-form :deep(.el-form-item) { margin-bottom: 20px; }
.auth-form :deep(.el-form-item__label) { font-weight: 650; color: #3b465a; }
@media(max-width: 760px) {
  .auth-shell { display: block; }
  .auth-story { min-height: 210px; padding: 25px; }
  .auth-content { margin: 30px 0 5px; }
  .auth-content h1 { font-size: 30px; margin: 8px 0; }
  .auth-content p { font-size: 12px; }
  .auth-lines, .auth-bottom { display: none; }
  .auth-form-wrap { padding: 42px 25px; }
}
</style>
