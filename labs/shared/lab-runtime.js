(() => {
  const q = (selector, root = document) => root.querySelector(selector);
  function graph(nodes, edges) {
    const positions = new Map();
    const dense = nodes.length > 6;
    const rx = dense ? 116 : 105, ry = dense ? 96 : 88, width = dense ? 94 : 108;
    nodes.forEach((node, index) => {
      const angle = Math.PI * 2 * index / nodes.length - Math.PI / 2;
      positions.set(node.id, {x: 160 + Math.cos(angle) * rx, y: 125 + Math.sin(angle) * ry});
    });
    return `<svg class="graph" viewBox="0 0 320 250" role="group" aria-label="Illustrative lesson flow"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" fill="#637b91"/></marker></defs>${edges.map(edge => {
      const a = positions.get(edge[0]), b = positions.get(edge[1]);
      return `<line class="edge" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>`;
    }).join('')}${nodes.map(node => {
      const point = positions.get(node.id);
      return `<g class="node${dense ? ' dense' : ''}" data-node="${node.id}" role="button" tabindex="0" aria-label="${node.label}" aria-pressed="false" transform="translate(${point.x - width / 2} ${point.y - 20})"><rect width="${width}" height="40" rx="12"/><text x="${width / 2}" y="24" text-anchor="middle">${node.label}</text></g>`;
    }).join('')}</svg>`;
  }
  function canvas() {
    const element = q('canvas'), ctx = element.getContext('2d');
    function size() {
      const scale = devicePixelRatio || 1, rect = element.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width * scale));
      const height = Math.max(1, Math.round(rect.height * scale));
      if (element.width !== width || element.height !== height) {
        element.width = width;
        element.height = height;
      }
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      return rect;
    }
    return {canvas: element, ctx, size};
  }
  function mount(config, start) {
    q('[data-title]').textContent = config.title;
    q('[data-subtitle]').textContent = config.subtitle;
    q('[data-learning]').textContent = config.learning;
    q('[data-level]').textContent = `Lab ${config.level}`;
    const specification = q('[data-spec]');
    config.spec.split(/(?<=\n)/).forEach(line => {
      const span = document.createElement('span');
      span.className = /^#/.test(line) ? 'spec-heading' : /^\d+\./.test(line) ? 'spec-step' : /^- /.test(line) ? 'spec-bullet' : 'spec-text';
      span.textContent = line;
      specification.appendChild(span);
    });
    const harness = config.harness;
    const contexts = config.canonical ? [
      ['context', 'Canonical project', `${harness.project} ${harness.version}; framework ${harness.framework_version}. All selected project authority is local.`, 'project/literate.project.json'],
      ['authored', 'Resolved Components', `${harness.component_count} actual locked Component(s). Graph nodes also show Flavors and pinned inputs; they are not all Components.`, `Component lock SHA-256 ${harness.lock_sha256}`],
      ['authored', 'Selected Flavors', harness.selected_flavors.join(' · '), 'Exact selections from component.lock.json'],
      ['shared', 'Pinned authoring inputs', 'The lock pins front-end and planning skills, workflow and routing. Select graph nodes or inspect their complete source below.', 'project/skills · project/workflows · project/routing'],
      ['shared', 'Retained source boundary', 'This initial implementation is authored retained input. HTML assembly does not claim model generation. Rebuild must independently validate and accept it.', 'project/retained-input'],
      ['context', 'Lifecycle evidence', 'Read project/verification/current.json for the actual qualification result. This preview does not embed or manufacture its future receipt. Changing authority makes old evidence stale.', 'project/verification/current.json']
    ] : [
      ['authored', 'Language choice', harness.language_selection, 'main.js'],
      ['shared', 'Shared implementation', 'Browser canvas, controls, source inspector and common styling are bundled, authored support.', 'labs/shared/lab-runtime.js · labs/shared/lab.css'],
      ['shared', 'HTML assembly', 'The Python builder inlines authored sources. It does not generate JavaScript from the specification.', 'labs/scripts/build_labs.py'],
      ['context', 'Project registration', `${harness.project} ${harness.version}: documentation root ${harness.documentation_root ? 'yes' : 'no'}; canonical Component root ${harness.component_root ? 'yes' : 'no'}. This describes this lab, not the whole framework.`, 'literate.project.json'],
      ['unselected', 'Flavor selection: none', 'The catalog contains lang-javascript, targeting Node.js 20+. It was not selected for this browser lab; its existence does not establish compatibility or a resolved contribution.', harness.available_flavor],
      ['unselected', 'Lifecycle evidence: absent', 'This bundle supplies no Component lock, selected skill/workflow/routing graph, generator-run evidence or independent lifecycle receipt. They are not hidden inputs.', 'Documentation example only']
    ];
    contexts.forEach(([kind, title, text, source]) => {
      const card = document.createElement('article');
      card.className = `harness-card ${kind}`;
      const heading = document.createElement('h3'); heading.textContent = title;
      const description = document.createElement('p'); description.textContent = text;
      const origin = document.createElement('code'); origin.textContent = source;
      card.append(heading, description, origin);
      q('[data-harness]').appendChild(card);
    });
    q('[data-spec-details]').open = config.level === '00';
    q('[data-inventory]').textContent = config.canonical
      ? `${harness.component_count} resolved Component(s) · ${harness.selected_flavors.length} selected Flavors · pinned skills/workflow/routing. These are different node kinds.`
      : `1 authored lab specification · ${config.nodes.length} illustrative roles. No resolved Component DAG is supplied.`;
    const container = q('[data-graph]');
    container.innerHTML = graph(config.nodes, config.edges);
    if (config.canonical) container.querySelector('svg').setAttribute('aria-label', 'Resolved authority graph');
    function select(event) {
      const node = event.target.closest('.node');
      if (!node) return;
      if (event.type === 'keydown') {
        if (!['Enter', ' '].includes(event.key)) return;
        event.preventDefault();
      }
      container.querySelectorAll('.node').forEach(element => {
        element.classList.toggle('active', element === node);
        element.setAttribute('aria-pressed', String(element === node));
      });
      q('[data-authority]').textContent = config.nodes.find(item => item.id === node.dataset.node).detail;
      q('.flash').classList.add('on');
      setTimeout(() => q('.flash').classList.remove('on'), 180);
    }
    container.addEventListener('click', select);
    container.addEventListener('keydown', select);
    const sourceList = q('[data-sources]');
    config.sources.forEach(source => {
      const details = document.createElement('details');
      details.dataset.sourcePath = source.path;
      const summary = document.createElement('summary');
      summary.textContent = source.path;
      const identity = document.createElement('p');
      identity.className = 'source-identity';
      identity.textContent = `SHA-256 ${source.sha256}`;
      const pre = document.createElement('pre');
      pre.className = 'spec source';
      pre.textContent = source.content;
      details.append(summary, identity, pre);
      sourceList.appendChild(details);
    });
    const api = {
      ...canvas(), controls: q('[data-controls]'), readout: q('[data-readout]'),
      log: value => q('[data-readout]').textContent = value,
      status: value => {
        let status = q('[data-media-status]');
        if (!status) {
          status = document.createElement('p'); status.dataset.mediaStatus = '';
          status.setAttribute('role', 'status'); api.controls.appendChild(status);
        }
        status.textContent = value;
      },
      button: (label, action, secondary = false) => {
        const button = document.createElement('button');
        button.textContent = label;
        button.className = secondary ? 'secondary' : '';
        button.onclick = action;
        api.controls.appendChild(button);
        return button;
      },
      slider: (min, max, value, action, name = null) => {
        const input = document.createElement('input');
        input.type = 'range'; input.min = min; input.max = max; input.value = value;
        if (name) input.setAttribute('aria-label', name);
        input.oninput = () => action(+input.value);
        if (name) {
          const label = document.createElement('label');
          label.className = 'range-control'; label.append(document.createTextNode(name), input);
          api.controls.appendChild(label);
        } else api.controls.appendChild(input);
        return input;
      },
      audio: () => new (window.AudioContext || window.webkitAudioContext)()
    };
    start(api);
  }
  window.LitLab = {mount};
})();
