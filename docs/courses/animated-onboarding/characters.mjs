// Project-owned SVG puppets. Visual identities follow the retained character brief;
// these are original articulated vectors, not edits to the retained PNG artwork.
export function character(id) {
  const sam = id === 'sam', shirt = sam ? '#283344' : '#154653', hair = sam ? '#422e27' : '#093744';
  const skin = sam ? '#e8ac7d' : '#efbc94', accent = sam ? '#d69b38' : '#47ac9f';
  return `<g id="${id}-rig" role="img" aria-label="${sam ? 'Sam, a skeptical engineer' : 'Literate AI, an engineering guide'}">
    <ellipse cx="0" cy="433" rx="108" ry="13" fill="#152c3a" opacity=".12"/>
    <g id="${id}-body">
      <path d="M-91 241 Q-75 211 -37 211 L37 211 Q75 211 91 241 L109 421 Q0 446 -109 421Z" fill="${shirt}" stroke="#16313a" stroke-width="4"/>
      <path d="M-34 211 L0 258 L34 211" fill="none" stroke="${accent}" stroke-width="${sam ? 7 : 16}"/>
      ${sam ? '<path d="M-103 401 Q0 426 103 401" fill="none" stroke="#354354" stroke-width="18"/>' : '<path d="M0 264 L0 426 M-38 219 L-62 291 L-18 276 M38 219 L62 291 L18 276" fill="none" stroke="#2d6873" stroke-width="6"/>'}
      <path d="M-77 249 Q-107 302 -88 355 L-30 363" fill="none" stroke="#16313a" stroke-width="42" stroke-linecap="round"/>
      <path d="M-77 249 Q-107 302 -88 355 L-30 363" fill="none" stroke="${shirt}" stroke-width="34" stroke-linecap="round"/>
      <path d="M-41 349 Q-22 344 -13 352 L0 365 Q-8 382 -35 377Z" fill="${skin}" stroke="#9f7059" stroke-width="2"/>
      <path d="M-55 354 L-56 374" stroke="${accent}" stroke-width="7"/>
    </g>
    <g id="${id}-head">
      ${sam ? '' : '<ellipse cx="31" cy="27" rx="44" ry="43" fill="#124c59" stroke="#102e38" stroke-width="3"/><path d="M10 3 Q53 -8 63 28 M14 13 Q43 5 56 39" fill="none" stroke="#287387" stroke-width="5" stroke-linecap="round"/>'}
      <path d="M-24 178 L-26 222 Q0 241 26 222 L24 178" fill="${skin}" stroke="#9f7059" stroke-width="3"/>
      <ellipse cx="0" cy="113" rx="80" ry="99" fill="${hair}"/>
      <ellipse cx="-69" cy="127" rx="14" ry="24" fill="${skin}"/>
      <ellipse cx="69" cy="127" rx="14" ry="24" fill="${skin}"/>
      <path d="M-66 73 Q-60 30 0 32 Q60 30 66 73 L62 148 Q52 196 0 207 Q-52 196 -62 148Z" fill="${skin}" stroke="#9f7059" stroke-width="3"/>
      ${sam ? '<path d="M-65 133 Q-50 155 -36 150 Q0 167 36 150 Q50 155 65 133 L58 166 Q44 202 0 209 Q-44 202 -58 166Z" fill="#54372c"/><path d="M-36 152 Q0 139 36 152 L33 164 Q0 153 -33 164Z" fill="#422e27"/>' : ''}
      <path d="M-76 105 Q-100 36 -49 12 Q-28 -12 7 8 Q60 -13 77 39 Q95 72 70 115 L57 82 Q25 71 9 44 Q-22 79 -65 81Z" fill="${hair}" stroke="#102e38" stroke-width="3"/>
      <path d="M-57 44 Q-13 7 38 27 M-60 65 Q-22 43 -5 24" fill="none" stroke="${sam ? '#78503c' : '#226879'}" stroke-width="7" stroke-linecap="round"/>
      <g id="${id}-eyes">
        <ellipse cx="-27" cy="119" rx="16" ry="10" fill="#fffaf2"/><ellipse cx="27" cy="119" rx="16" ry="10" fill="#fffaf2"/>
        <g id="${id}-pupils"><circle cx="-27" cy="119" r="7" fill="${sam ? '#56463c' : '#278f92'}"/><circle cx="27" cy="119" r="7" fill="${sam ? '#56463c' : '#278f92'}"/><circle cx="-27" cy="119" r="3.6" fill="#142c34"/><circle cx="27" cy="119" r="3.6" fill="#142c34"/><circle cx="-29" cy="116" r="2" fill="white"/><circle cx="25" cy="116" r="2" fill="white"/></g>
      </g>
      ${sam ? '' : '<path d="M-44 118 L-49 112 M44 118 L49 112" stroke="#16313a" stroke-width="3" stroke-linecap="round"/>'}
      <g id="${id}-brows" stroke="${hair}" stroke-width="7" stroke-linecap="round"><path d="M-44 99 Q-29 92 -13 98" fill="none"/><path d="M13 98 Q29 92 44 99" fill="none"/></g>
      <path d="M0 122 L-6 144 Q0 150 9 144" fill="none" stroke="#b57f64" stroke-width="3" stroke-linecap="round"/>
      <ellipse id="${id}-mouth" cx="0" cy="172" rx="17" ry="2" fill="#56372f"/>
      <path id="${id}-smile" d="M-18 171 Q0 181 18 171" fill="none" stroke="${sam ? '#a6755c' : '#b76458'}" stroke-width="3" stroke-linecap="round"/>
    </g>
  </g>`;
}

// Foreground arms share the body's palette but are composited above the work
// surface, folder and preview so reaching cannot disappear behind a panel.
export function arm(id) {
  const sam = id === 'sam', shirt = sam ? '#283344' : '#154653';
  const skin = sam ? '#e8ac7d' : '#efbc94', accent = sam ? '#d69b38' : '#47ac9f';
  return `      <g id="${id}-arm">
        <path id="${id}-arm-outline" d="M73 249 Q107 269 110 307 Q111 330 ${sam ? 50 : 91} 350" fill="none" stroke="#16313a" stroke-width="35" stroke-linecap="round" stroke-linejoin="round"/>
        <path id="${id}-sleeve" d="M73 249 Q107 269 110 307 Q111 330 ${sam ? 50 : 91} 350" fill="none" stroke="${shirt}" stroke-width="28" stroke-linecap="round" stroke-linejoin="round"/>
        <g id="${id}-hand" transform="translate(${sam ? 50 : 91} 350)">
          <path d="M-11 -6 Q-13 5 -7 15 Q-2 24 7 20 L14 10 Q18 1 10 -5Z" fill="${skin}" stroke="#9f7059" stroke-width="2"/>
          <path d="M-9 0 Q-20 -2 -18 6 L-9 14" fill="${skin}" stroke="#9f7059" stroke-width="2"/>
          <path d="M-12 -6 L11 -6" stroke="${accent}" stroke-width="7"/>
        </g>
      </g>
`;
}
