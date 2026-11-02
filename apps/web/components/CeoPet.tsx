"use client";

/**
 * 商务范儿 CEO 小人（纯展示组件）。
 *
 * 画法刻意保持"西装 + 领带 + 细框眼镜"的商务感，而不是动物或萌系角色：
 * 老板要的是一个能代表公司 CEO 的常驻形象，出现在任何页面都不违和。
 *
 * 动画全部交给 globals.css 里的 `ceo-pet-*` 类：
 * 呼吸起伏、定时眨眼、悬停挥手；点击时的弹跳由外层容器控制。
 * `prefers-reduced-motion` 下这些动画统一关闭。
 */
export default function CeoPet({ size = 84 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={Math.round(size * 1.25)}
      viewBox="0 0 96 120"
      role="img"
      aria-label="CEO"
      style={{ display: "block", overflow: "visible" }}
    >
      <defs>
        <linearGradient id="ceo-suit" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2a3854" />
          <stop offset="100%" stopColor="#16213c" />
        </linearGradient>
        <linearGradient id="ceo-head" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#fbd6b4" />
          <stop offset="100%" stopColor="#efbc95" />
        </linearGradient>
      </defs>

      {/* 影子 */}
      <ellipse cx="48" cy="113" rx="25" ry="4.5" fill="rgba(0,0,0,0.28)" />

      {/* 西裤与皮鞋 */}
      <path d="M37 92h9v14h-9z" fill="#1b2740" />
      <path d="M50 92h9v14h-9z" fill="#1b2740" />
      <rect x="32" y="103" width="14" height="8.5" rx="3.6" fill="#0d1322" />
      <rect x="50" y="103" width="14" height="8.5" rx="3.6" fill="#0d1322" />

      {/* 手臂先画，西装盖在肩线上 */}
      <path d="M33 62q-8 6-8 16v10q0 4 4 4t4-4l1-16z" fill="url(#ceo-suit)" />
      <circle cx="29" cy="92" r="4.2" fill="url(#ceo-head)" />

      <g className="ceo-pet-wave">
        <path d="M64 62q8 2 9-6l2-10q1-5-3-6t-5 4l-2 12-3 14z" fill="url(#ceo-suit)" />
        <circle cx="75" cy="40" r="4.5" fill="url(#ceo-head)" />
      </g>

      {/* 西装外套 */}
      <path
        d="M30 63q0-8 8-10h20q8 2 8 10l3 30q0 7-7 7H34q-7 0-7-7z"
        fill="url(#ceo-suit)"
      />

      {/* 衬衫 + 领带 */}
      <path d="M40 53l8 13 8-13z" fill="#f1f5f9" />
      <path d="M46 57h4l1.6 4.5L48 66l-3.6-4.5z" fill="#d4a24c" />
      <path d="M46 63h4l2 19-4 6-4-6z" fill="#d4a24c" />

      {/* 翻领 */}
      <path d="M39 53l9 11-4.5 20-8-23z" fill="#33425e" />
      <path d="M57 53l-9 11 4.5 20 8-23z" fill="#33425e" />

      {/* 扣子与口袋巾 */}
      <circle cx="48" cy="84" r="1.35" fill="#8b9bb8" />
      <circle cx="48" cy="91" r="1.35" fill="#8b9bb8" />
      <rect x="55" y="74" width="6.5" height="2.2" rx="1.1" fill="#d4a24c" opacity="0.9" />

      {/* 脖子与头 */}
      <rect x="43" y="44" width="10" height="10" rx="3.2" fill="#e9b993" />
      <circle cx="30.5" cy="35" r="3.6" fill="#efbc95" />
      <circle cx="65.5" cy="35" r="3.6" fill="#efbc95" />
      <rect x="31" y="17" width="34" height="35" rx="14" fill="url(#ceo-head)" />

      {/* 短发 */}
      <path
        d="M31 32q0-15 17-15t17 15q0-7-6-9.5-8-3-13-1.5-9 2.5-15 11z"
        fill="#20242f"
      />

      {/* 细框眼镜 + 眼睛 */}
      <ellipse cx="41.5" cy="35.5" rx="2.2" ry="2.7" fill="#20242f" className="ceo-pet-eye" />
      <ellipse cx="54.5" cy="35.5" rx="2.2" ry="2.7" fill="#20242f" className="ceo-pet-eye" />
      <g stroke="#c7d2fe" strokeWidth="1.15" fill="none" opacity="0.85">
        <circle cx="41.5" cy="35.5" r="4.4" />
        <circle cx="54.5" cy="35.5" r="4.4" />
        <path d="M45.9 35.2h4.2" />
        <path d="M31.2 34.4h5.9" />
        <path d="M58.9 34.4h5.9" />
      </g>

      {/* 鼻子、嘴、耳机麦克风 */}
      <path d="M48 35.5q1.4 2.2 0 3.8" stroke="#d9a47f" strokeWidth="1.2" fill="none"
        strokeLinecap="round" />
      <path d="M44.3 42.2q3.7 2.6 7.4 0" stroke="#b9744f" strokeWidth="1.4" fill="none"
        strokeLinecap="round" />
      <path d="M66 36q3.5 7-4 10" stroke="#60a5fa" strokeWidth="1.1" fill="none"
        opacity="0.75" strokeLinecap="round" />
      <circle cx="66" cy="36" r="1.5" fill="#60a5fa" opacity="0.9" />
    </svg>
  );
}
