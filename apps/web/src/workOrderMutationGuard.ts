/** Same-work-order intent ownership only; unrelated jobs and safety commands remain independent. */
export class WorkOrderMutationGuard {
  private readonly leases = new Map<string, Set<string>>()
  blocked(orderId: string): boolean { return (this.leases.get(orderId)?.size ?? 0) > 0 }
  acquire(orderId: string, owner: 'workflow' | 'cancellation', requestId: string): boolean {
    if (this.blocked(orderId)) return false
    this.retain(orderId, owner, requestId)
    return true
  }
  retain(orderId: string, owner: 'workflow' | 'cancellation', requestId: string) {
    const identities = this.leases.get(orderId) ?? new Set<string>()
    identities.add(`${owner}:${requestId}`)
    this.leases.set(orderId, identities)
  }
  release(orderId: string, owner: 'workflow' | 'cancellation', requestId: string) {
    const identities = this.leases.get(orderId)
    identities?.delete(`${owner}:${requestId}`)
    if (identities?.size === 0) this.leases.delete(orderId)
  }
}
