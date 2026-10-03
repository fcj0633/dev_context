## 整体结论：占座一致性由哪些机制协同保证
当前购票占座的一致性由多层机制协同保证。入口 purchaseTickets 先经 tokenBucket.takeToken 预筛，扣减失败直接抛“列车站点已无余票”，成功才进入本地占座流程 [E10]。并发写由 Redisson 用户锁与席别锁串行化，核心座位状态更新在本地事务 doPurchaseInTransaction 中执行 [E5] [E9]。座位以条件更新 AVAILABLE→LOCKED 并校验影响行数是否等于应占座位数，防止重复占座 [E9]。回调侧统一用“条件更新+影响行数”实现幂等，只有取得推进权的回调才能修改座位 [E12]。因此，入口控量、锁串行化、数据库条件更新原子判定、本地事务回滚、回调幂等共同构成一致性保障。

## 座位状态定义与占座一致性约束
SeatDO 中 seat_status 的定义是：0 可售、1 已锁定、2 已出售，座位绑定一个发售区间 [E3]。占座的合法流转是 AVAILABLE(0)→LOCKED(1)。doPurchaseInTransaction 将 seatStatus 设置为 LOCKED，并在 WHERE 中限定 seat_status=AVAILABLE，随后校验 affectedRows 是否等于 seats.size() [E9]。若分配座位数量不足，或条件更新影响行数不符，代码会抛出“站点余票不足”异常并触发事务回滚 [E9]。因此，占座成功必须同时满足状态合法与数量一致，否则整个本地事务不提交。

## 从入口到存储层的调用链路
接口 PurchaseTicketService#purchaseTickets 定义购票入口 [E1]。实现先执行链式校验、用户校验和乘车人数量校验，再 buildSeatTypeCounts，并调用 tokenBucket.takeToken 做余票令牌预筛；失败即抛“列车站点已无余票” [E10]。成功后 loadPassengers、生成 orderSn，进入 reserveLocally 加锁，再调用 doPurchaseInTransaction 访问 seat/ticket 存储 [E10] [E5] [E9]。本地事务提交后删除余票缓存并创建订单；失败分支触发补偿或令牌归还 [E10]。该链路把入口控量、并发串行化和本地原子写依次串起来。

## 并发冲突处理：用户锁与席别锁串行化
reserveLocally 先获取用户锁，key 包含 username 和 trainId，再按 seatTypeCounts 逐个获取席别锁，key 包含 trainId 和席别 [E5]。这些锁覆盖 doPurchaseInTransaction 调用，形成串行化临界区。finally 中倒序释放已获取的席别锁，再释放用户锁，释放使用 unlockSafely [E5]。PurchaseTicketTxService 类注释明确：调用方已经完成乘车人校验并持有用户锁和全部席别锁；本事务只操作 ticket 数据库，远程调用留在编排层 [E6]。这样可避免同一用户或同一车次席别同时进入占座核心写区。

## 事务边界与原子性保障
doPurchaseInTransaction 标注 @Transactional(rollbackFor=Exception.class)，座位状态更新与 TicketDO 插入位于同一本地事务内 [E9]。事务内按席别分配座位、条件更新座位状态、插入 TicketDO，并构建订单创建请求和补偿请求 [E9]。PurchaseTicketTxService 类注释说明：本事务只操作 ticket 数据库；远程调用留在编排层，避免网络等待延长行锁持有时间 [E6]。因此原子性范围覆盖 ticket 库内的座位与车票写入，远程交互不放进该事务边界。

## 防止超卖与重复占座：条件更新+影响行数
座位更新条件写入 WHERE：id 在待占座位集合中且 seat_status=AVAILABLE [E9]。更新目标为 LOCKED；若 affectedRows 不等于 seats.size()，抛“站点余票不足”异常 [E9]。SeatDO 状态 0 可售、1 已锁定、2 已出售 [E3]。这种条件更新把判断与修改合并到数据库原子操作，避免先查再改时两个请求都查到可售并先后修改的问题 [E12]。因此，防超卖与防重复占座的关键不是应用层查一次，而是数据库条件更新加影响行数校验。

## 异常回滚与锁释放
事务内异常，例如座位不足、影响行数不符、乘车人不存在等，会触发 @Transactional 回滚，座位更新和车票插入一起回滚 [E9]。reserveLocally 的 finally 会倒序释放已获取的席别锁，再释放用户锁，释放使用 unlockSafely [E5]。本地占座已提交后，若余票缓存删除失败，只记录警告并等待缓存过期，不改变已提交的本地占座结果 [E7]。因此，失败路径先保证数据库回滚，再保证锁资源释放；提交后的缓存删除属于降级处理。

## 回调侧幂等：条件更新+影响行数
TicketCallbackServiceImpl 将幂等统一实现为“条件更新+影响行数”：车票先以 orderSn 从未支付推进，只有取得推进权的回调才能修改座位 [E12]。座位只在“已锁定”时才能被推进；失败时回滚本次车票状态变更；影响行数为 0 不抛异常，表示跨服务重试中已经处理过 [E12]。取消回调接口 cancelCallback 是回调入口之一 [E2]。之所以不用“先查再改”，是因为查询与修改之间存在并发窗口；把判断条件写进 WHERE，判断与修改就在数据库里原子完成 [E12]。

## 令牌桶预筛与令牌归还时序
purchaseTickets 先调用 tokenBucket.takeToken，扣减成功才继续；失败直接抛“列车站点已无余票” [E10]。finally 中若本地占座未提交 reservationCommitted=false，则归还令牌；本地事务提交后，取消回调成为唯一令牌归还方 [E10]。回调侧 returnTokensAfterCommit 通过事务同步 afterCommit 归还令牌 [E8]。returnToken 异常时记录警告，保留偏松降级并等待 TTL 自愈 [E4]。因此，令牌桶既做入口预筛，也按提交状态划分归还责任，异常归还走降级自愈。

## 跨服务失败补偿与未知结果处理
建单明确失败时调用 compensateKnownFailure，通过 cancelCallback 释放座位并归还令牌；补偿失败记录日志并等待恢复任务 [E11]。若订单返回成功但 orderSn 不一致，视为结果未知，不释放座位，抛“订单创建结果未知，系统将自动核对”，避免已支付座位被重新售卖 [E10]。本地占座提交后删除余票缓存，删除失败仅告警等待缓存过期 [E7]；令牌归还异常也走降级自愈 [E4]。这套处理区分已知失败与未知结果，减少座位泄漏和重复售卖风险。

## 一致性边界与降级行为
本地事务内座位条件更新加影响行数校验是强一致判定，只有影响行数等于应占座位数才提交 [E9]。回调侧同样用条件更新加影响行数作为幂等判定，影响行数为 0 不抛异常 [E12]。缓存删除失败、令牌归还异常、补偿失败均不阻塞主流程，分别等待缓存过期、TTL 自愈或恢复任务，属于最终一致或降级处理 [E7] [E4] [E11]。现有证据未显示依赖数据库唯一约束或版本号；当前防重复占座的核心是条件更新与影响行数校验 [E9] [E12]。
