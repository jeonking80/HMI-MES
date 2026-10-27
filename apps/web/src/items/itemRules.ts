export const UNITS = ['개', '박스', '세트', 'kg', 'g', 'm', 'L']
/** 규격 품목 ID: ITM- + 4자리 일련번호. 기존 ITM-nnnn 중 가장 큰 번호 다음. */
export function nextItemId(items: { id: string }[]) {
  const used = items.map(item => /^ITM-(\d{4})$/.exec(item.id)?.[1]).filter(Boolean).map(Number)
  return `ITM-${String((used.length ? Math.max(...used) : 0) + 1).padStart(4, '0')}`
}

