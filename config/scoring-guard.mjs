// Last-layer guard for DeepSeek Harness. Resume evaluation has no tool side effects.
export const name = 'recruit-scoring-guard';
export const inject = ['tools'];
export function apply(ctx) {
  ctx.tools.guard(() => 'RECRUIT_SCORING_TOOLS_DISABLED: use only the supplied job and resume evidence.');
}
