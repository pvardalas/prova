const Q=new URLSearchParams(location.search);const D=parseFloat(Q.get('d')||'10');
const steps=[];function T(f,fn){steps.push([f,fn])}
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
function on(sel){$$(sel).forEach(e=>e.classList.add('on'))}
function seq(sel,f0,f1){const els=$$(sel);els.forEach((e,i)=>T(f0+(f1-f0)*i/Math.max(els.length-1,1),()=>e.classList.add('on')))}
function typeText(el,text,secs){let i=0;const n=text.length,dt=secs*1000/n;const iv=setInterval(()=>{el.textContent=text.slice(0,++i);if(i>=n)clearInterval(iv)},dt)}
window.go=()=>{document.body.classList.add('go');for(const [f,fn] of steps)setTimeout(fn,f*D*1000)};
// ?static=1 shows the final state (for quick visual checks)
window.addEventListener('load',()=>{if(Q.get('static')){steps.sort((a,b)=>a[0]-b[0]).forEach(([f,fn])=>fn());document.querySelectorAll('.a').forEach(e=>e.classList.add('on'))}});
