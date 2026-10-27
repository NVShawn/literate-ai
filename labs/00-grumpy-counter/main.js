window.startLab = api => {
  let modulus = 37;
  let frame = 0;
  const label = document.createElement('label');
  label.className = 'range-control';
  label.htmlFor = 'modulus';
  label.textContent = 'Modulus ';
  const value = document.createElement('output');
  value.htmlFor = 'modulus';
  label.appendChild(value);
  api.controls.appendChild(label);
  const slider = api.slider(7, 97, modulus, next => {
    modulus = next;
    updateReadout();
  });
  slider.id = 'modulus';
  slider.step = '1';
  slider.setAttribute('aria-describedby', 'modulus-help modulus-legend');
  label.appendChild(slider);
  const help = document.createElement('p');
  help.id = 'modulus-help';
  help.textContent = 'Change the modulus to rearrange remainder rings. Prime classification stays the same.';
  const legend = document.createElement('p');
  legend.id = 'modulus-legend';
  legend.textContent = 'Prime · teal; Composite · orange; 1 · gray';
  api.controls.append(help, legend);

  function updateReadout() {
    value.textContent = String(modulus);
    api.log(`modulus ${modulus} · primes remain smug`);
  }
  function prime(n) {
    if (n < 2) return false;
    for (let divisor = 2; divisor * divisor <= n; divisor++) {
      if (n % divisor === 0) return false;
    }
    return true;
  }
  function draw() {
    const rect = api.size();
    const {ctx} = api;
    ctx.fillStyle = '#050a10';
    ctx.fillRect(0, 0, rect.width, rect.height);
    ctx.save();
    ctx.translate(rect.width / 2, rect.height / 2);
    for (let n = 1; n < 240; n++) {
      const isPrime = prime(n);
      const angle = n * .31 + frame * .002;
      const radius = 22 + (n % modulus) * Math.min(rect.width, rect.height) * .006;
      ctx.fillStyle = n === 1 ? '#9db0c4' : isPrime ? '#38d6c2' : '#ff8a3d';
      ctx.globalAlpha = n === 1 ? .55 : isPrime ? .9 : .35;
      ctx.beginPath();
      ctx.arc(Math.cos(angle) * radius, Math.sin(angle) * radius, isPrime ? 3.4 : 2, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
    frame++;
    requestAnimationFrame(draw);
  }
  updateReadout();
  draw();
};
