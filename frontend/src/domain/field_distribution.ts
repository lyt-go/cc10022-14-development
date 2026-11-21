/** 字段归属与分发明细的前端领域描述：与后端判定词保持一致，页面不必各写一遍。 */

export type OwnershipDecision = '可采纳' | '拒绝' | '待核' | '一致' | '无回传'

export type DistributionItem = {
  id?: number
  明细编号: string
  回执编号: string
  黄金ID: string
  目标系统: string
  字段: string
  本地值: string
  远程值: string
  维护归属: string
  主源系统: string
  判定: OwnershipDecision
  确认状态: string
}

/** 可采纳与拒绝必须能对回本地值和远程值，页面据此展示对照列。 */
export function valuePair(item: DistributionItem): { local: string; remote: string } {
  return { local: item.本地值 ?? '', remote: item.远程值 ?? '' }
}

/** 只有确认采纳且目标有维护归属的条目允许写回黄金记录。 */
export function canWriteBack(item: DistributionItem): boolean {
  return item.判定 === '可采纳' && item.确认状态 === '已采纳'
}

export const DECISION_ORDER: OwnershipDecision[] = ['可采纳', '拒绝', '待核', '一致', '无回传']

export const DECISION_LABEL: Record<OwnershipDecision, string> = {
  可采纳: '目标维护该字段，远程值可采纳',
  拒绝: '目标无权维护，保留主数据组的值',
  待核: '归属未登记，需复核员判定',
  一致: '本地值与远程值一致，无需变更',
  无回传: '目标没有回传该字段',
}
