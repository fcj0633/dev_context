## 总体结论：幂等由“唯一标识 + 先查后写 + 条件更新”共同保证

当前订单和支付链路的幂等，核心是把 orderSn/paySn 当作状态机的唯一入口：同一请求只有第一次能从预期前置状态迁移成功，后续重放都被识别为“已处理”，不会产生第二次业务副作用。[E4][E2]

具体分三层兜底。第一层是“先查后写”：createTicketOrder 先按 orderSn 查询已有订单，存在则调用 assertSameCreateRequest 比对用户、车次、席别、身份证、金额等字段，完全一致才返回原 orderSn，不一致则抛“订单号已被其他购票请求占用”。[E4][E3] 第二层是“条件更新 + 影响行数”：订单侧 markOrderPaid、closePendingOrder，支付侧把 paySn + status=WAIT_PAY 作为更新条件，只有影响行数大于 0 才算首次推进；为 0 只记录“已被并发处理或已关闭”。[E2][E10] 第三层是数据库唯一键兜底：支付单创建依赖 t_pay.order_sn 的 UNIQUE KEY，避免同一订单产生多笔待支付交易。[E17]

支付回调侧同样如此：payCallbackOrder 先判订单是否已是 ALREADY_PAID，是则直接 return true 让支付服务停止重推；正常路径的条件更新若影响行数为 0，也按“已处理”对待。[E6] 因此整条链路的重试是安全的重放，而不是重复扣款或重复关单。

## 订单创建幂等：orderSn 复用与同请求校验

订单创建阶段用 orderSn 作为幂等键，拦截重复提交。createTicketOrder 的第一步就是 selectByOrderSn(orderSn)：若订单已存在，不会重新插入，而是调用 assertSameCreateRequest 做“同一请求”判定。[E4]

assertSameCreateRequest 的比对分两部分：头部字段比对 userId、username、trainId、departure、arrival；明细部分按 orderSn 查出已有订单项，要求数量一致，并且请求中的每一项都能在已有项中找到 carriageNumber、seatNumber、seatType、idCard、amount 全部相等的记录。任何一处不同都会抛 ServiceException（“订单号已被其他购票请求占用”）。[E3] 这意味着 orderSn 相同但内容不同的请求不会被误当成重试复用。

并发场景由唯一键串行化。插入路径用 try/catch 捕获 DuplicateKeyException：若两个相同请求同时到达，只有一个能插入成功，另一个走异常分支重新 selectByOrderSn，再做一次 assertSameCreateRequest，通过则返回同一 orderSn，否则抛出原异常。[E4]

另外，订单明细随订单创建写入，延迟关单任务在事务提交后才投递，注释说明这是为了避免回滚后留下“关一笔不存在订单”的脏任务。[E4][E5]

## 订单状态推进幂等：独立事务 + 条件更新 + 影响行数

订单本地状态的推进被单独抽到 OrderStateService，注释给出的理由是“本地状态必须比跨服务通知先提交”。关单链路是“先关订单 → 再关支付单 → 最后回滚座位”，后两步是跨服务调用，可能超时或失败；如果三步放在同一事务，下游失败会回滚本地关单，重试要从头再来，还可能出现“座位已释放、订单还能支付”的超卖窗口。[E2]

因此“改本地状态”被做成独立事务方法：它先提交，之后才允许通知下游。重试时通过“条件更新 + 影响行数”判断是否已经推进过，重复调用天然被识别为已处理。[E2]

OrderServiceImpl 的类注释也明确：P2 的支付回调、超时关单、用户主动取消复用同一套幂等逻辑，状态推进全部靠“条件更新 + 影响行数”，重复调用只会被识别为“已处理”，不会造成二次破坏。[E5]

OrderStateService 暴露 closePendingOrder 与 markOrderPaid 两个方法，分别承担待支付到已取消、待支付到已支付的条件迁移。支付回调在条件更新返回 false 时的处理正是依赖这一语义：并发下被其他线程先推进，也只记录 info 日志并视为已处理。[E6]

## 订单侧支付回调幂等：状态检查优先，条件更新兜底

订单侧 payCallbackOrder 的入参是 TicketOrderPayCallbackReqDTO。方法先校验 orderSn 非空，再 selectByOrderSn 查订单；查不到直接抛“订单不存在”。[E6]

接着是两条短路分支。第一，若订单状态已是 ALREADY_PAID，注释写明“重复回调：状态已是已支付，直接返回‘已处理’，让支付服务停止重推”，方法 return true。[E6] 第二，若订单状态是 CLOSED 却又收到支付成功，会记录 error 日志“订单已关闭却收到支付成功回调，需人工核对”，并同样 return true，表示“收到了、不会再重推”，真实退款被标注为 P3 后续处理。[E6][E18]

只有处于待支付状态时，才调用 orderStateService.markOrderPaid(orderSn, payType, payTime) 走条件更新。若返回 false，说明并发下被其他线程先推进（例如重复回调），同样按已处理对待，只记录 info 日志，方法最终仍返回 true。[E6]

这里的返回语义很关键：true 不代表“这次真的改了状态”，而是“这次回调已经被消化，上游可以停止重推”，这正是幂等入口的意义。[E6][E8]

## 支付单创建幂等：orderSn 唯一键 + 先查 Pay

支付单创建阶段的幂等由数据库唯一键与业务层先查共同保证。设计文档给出场景：用户连续发 5 次 POST /pay/create，不能产生“一张订单出现多笔待支付交易”的结果，因此 t_pay.order_sn 上设置了 UNIQUE KEY。[E17]

文档同时说明业务层会“先按 orderSn 查 Pay”，如果已经存在，则复用已有支付单（文档给出的设计意图到此为止，具体复用代码未在现有证据中展开，不能补造实现细节）。[E17]

支付动作本身还有状态闸门。checkOrderCanPay 只允许 PENDING_PAYMENT 状态通过；若订单已是 ALREADY_PAID，抛“订单已支付，无需重复支付”；其余情况抛“订单已取消，无法支付”。[E12] 这把“重复支付同一订单”挡在支付单创建与支付发起之前。

PayServiceImpl 的类注释补充了三点设计要点：金额只由服务端根据订单明细汇总，前端传的金额一律不信；所有状态推进都用“条件更新 + 影响行数”实现幂等，不依赖 Redis；支付成功后的下游通知失败不回滚支付单，只把通知状态置为未完成，由补偿任务重推。[E11] 因此支付单创建的幂等并不依赖额外的锁服务。[E11][E17]
