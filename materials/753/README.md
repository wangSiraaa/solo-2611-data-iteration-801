提供合成源文、实际译文、操作和只读客户端，没有覆盖或交付实现。
python3 package_reader.py example_package.json
样例是独立手写产品，非已经发布的历史包。translation-package/1给package_id/document_id/language、按源序source_snapshot（segment_id/revision）及按输出顺序chunks（translation_id/source_ids/text），可增字段但保留含义。

EN初始至少两种合法覆盖：T12+T3+T4，或T1+T23+T4。FR的F12/F23/F4虽并集含四段，但前两条重叠S2且都不能拆开，故无完整覆盖。
更正S2到修订2后，T12/T23/F12/F23均不再当前有效；旧PACK1保持源修订1文本。补T2_NEW后，EN可用T1+T2_NEW+T3+T4实际交付新文本。
只比较覆盖身份和精确源修订，不要求判断翻译语义。禁止把旧译文改标签充当补译。
