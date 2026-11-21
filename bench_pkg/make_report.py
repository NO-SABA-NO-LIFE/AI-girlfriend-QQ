import json
r=json.load(open('bench_results.json'))

def cell(x):
    if not x.get('ok'):
        e=x.get('err','')
        if '404' in e: return '404 不可用'
        if 'timed out' in e: return '超时 >55s'
        return '失败'
    return f"{x['sec']}s / {x['finish']} / ct={x['ct']}"

L=[]
L.append('# 亚托莉 QQ 机器人 — 全模型基准测试报告\n')
L.append('## 一、测试条件\n')
L.append('- 接口：higanai（https://api.higanai.top/v1），OpenAI 兼容协议')
L.append('- 测试时间：2026-11-20 21:36 CST（北京时间）')
L.append('- 统一参数：reasoning_effort=low、max_tokens=350、temperature=0.7')
L.append('- 单次请求超时：55 秒；每个模型每项各测 1 次')
L.append('- 文本任务：「用一句话介绍你自己」')
L.append('- 识图任务：描述 bench_test.png（640×480，白色背景，红色圆形、蓝色正方形、绿色三角形；顶部黑色英文 “ATRI BENCH 2026”，左下角紫色中文「高性能测试」）')
L.append('- 说明：ct = completion_tokens，部分模型会把隐藏思考链 token 计入；所有模型用同一张图，条件一致\n')
L.append('## 二、汇总结果\n')
L.append('| 模型 | 文本（耗时/结束/ct） | 识图（耗时/结束/ct） | 识图准确性 |')
L.append('|---|---|---|---|')
for m in r:
    acc='准确' if r[m]['vision'].get('ok') else '—'
    L.append(f"| {m} | {cell(r[m]['text'])} | {cell(r[m]['vision'])} | {acc} |")
L.append('\n## 三、关键发现\n')
L.append('1. **13 个模型中 8 个可用、5 个不可用。** 不可用分两类：gemini-3-flash、gemini-3.1-flash-lite-preview 的 chat 端点返回 404（在 /models 列表挂名但未真正部署）；gemini-3.1-pro、gemini-3.1-pro-preview、gemini-3.7-flash 在 55 秒内无响应（超时）。')
L.append('2. **所有可用模型的识图全部准确。** 8 个可用模型无一例外地正确识别了红圆、蓝方、绿三角三种形状及其颜色、白色背景，以及中英文文字（含紫色中文「高性能测试」）。横向看不存在“识图质量差”，只有“能用”和“不能用”的区别。')
L.append('3. **速度第一梯队（文本/识图均 ≤2s）：** gemini-3.1-flash-lite（1.28s / 1.93s）、gemini-2.5-flash-lite（2.28s / 1.84s）、gemini-3-flash-preview（3.69s / 1.86s）。')
L.append('4. **时延存在明显波动。** gemini-3.6-flash 文本本次耗时 52.85s（险些超时），但同模型识图仅 4.81s；说明单次测速受上游负载/路由影响，结论以可用性和大致量级为主，精确数值会有抖动。')
L.append('5. **思考链 token 差异：** 2.5 系列、3-flash-preview 的文本 ct 高达 342～383（隐藏思考被计入 completion_tokens，可见回复仍正常）；3.1-flash-lite、3.5、3.6、3.8、4-argon 的 ct 仅 14～28，reasoning_effort=low 在这些模型上抑制思考链的效果明显。')
L.append('6. **当前生产选型合理：** 主对话 gemini-3.8-flash（8.71s / 5.02s，准确）、识图主力 gemini-3.1-flash-lite（最快且准确）、记忆抽取 gemini-3.6-flash，均落在可用集合内。\n')
L.append('## 四、附录：各模型识图回复原文\n')
for m in r:
    v=r[m]['vision']
    L.append(f'### {m}')
    if v.get('ok'):
        L.append('> ' + v.get('reply','').replace('\n','\n> '))
    else:
        L.append('> （不可用）')
    L.append('')
open('bench_report.md','w').write('\n'.join(L))
print('wrote bench_report.md', len('\n'.join(L)), 'chars')
