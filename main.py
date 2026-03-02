import random
from datetime import datetime, timedelta
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.permission import PermissionType
import asyncio
import json
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import zoneinfo

# 默认生日祝福（LLM 不可用时的备用）
DEFAULT_GREETINGS = [
    "🎂 生日快乐！愿你的每一天都充满阳光和欢笑！",
    "🎉 今天是你的生日！祝你心想事成，万事如意！",
    "🎈 Happy Birthday！愿你永远年轻，永远快乐！",
    "🎁 生日快乐！希望新的一岁里，你能收获满满的幸福！",
    "🌟 祝你生日快乐！愿所有美好都如期而至！",
    "🎊 又长大一岁啦！祝你生日快乐，前途似锦！",
    "🎂 生日快乐！愿你的人生如同今天一样精彩！",
    "✨ 今天是属于你的日子！祝你生日快乐，天天开心！",
    "🎈 生日快乐！愿你拥有一切你所向往的美好！",
    "🎉 祝你生日快乐！新的一岁，新的开始，加油！",
]


@register(
    "astrbot-plugin-HappyBirthday",
    "204343414",
    "QQ好友生日自动祝福插件 - 每天检查好友生日并发送AI生成的个性化祝福",
    "1.0.0",
    "https://github.com/204343414/astrbot-plugin-HappyBirthday",
)
class BirthdayGreeter(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # ==================== 读取配置 ====================
        self.birthday_check_enabled: bool = config.get("birthday_check_enabled", True)
        self.send_hour: int = config.get("send_hour", 8)
        self.send_minute: int = config.get("send_minute", 0)
        self.check_hour: int = config.get("check_hour", 7)
        self.check_minute: int = config.get("check_minute", 30)
        self.use_llm_greeting: bool = config.get("use_llm_greeting", True)
        self.notify_groups: list[str] = config.get("notify_groups", [])
        self.blacklist_users: list[str] = config.get("blacklist_users", [])
        self.greeting_interval: int = config.get("greeting_interval", 5)

        # ==================== 数据持久化 ====================
        data_dir = StarTools.get_data_dir("astrbot-plugin-HappyBirthday")
        self.store_path = data_dir / "birthday_data.json"
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.store_path.exists():
            self.store_path.write_text("{}", encoding="utf-8")

        store_data = self._load_store_data()
        self.last_check_date: str = store_data.get("last_check_date", "2025-01-01")
        self.birthday_cache: dict = store_data.get("birthday_cache", {})
        self.greeted_today: dict = store_data.get("greeted_today", {})

        # ==================== 定时调度器 ====================
        tz = self.context.get_config().get("timezone")
        self.timezone = zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        self.is_checking: bool = False

        # 安排定时任务
        self._schedule_jobs()

        logger.info("🎂 生日祝福插件初始化完成")
        logger.info(f"📅 上次检查日期: {self.last_check_date}")
        logger.info(f"🔍 检查时间: {self.check_hour}:{self.check_minute:02d}")
        logger.info(f"🎉 发送时间: {self.send_hour}:{self.send_minute:02d}")
        logger.info(f"🤖 使用LLM生成祝福: {'是' if self.use_llm_greeting else '否'}")
        logger.info(f"📊 已缓存生日: {len(self.birthday_cache)} 人")

    # ================================================================
    #                        数据持久化
    # ================================================================

    def _load_store_data(self) -> dict:
        """加载存储数据"""
        try:
            with self.store_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载生日数据失败: {e}")
            return {}

    def _save_store_data(self):
        """保存存储数据"""
        try:
            data = {
                "last_check_date": self.last_check_date,
                "birthday_cache": self.birthday_cache,
                "greeted_today": self.greeted_today,
            }
            with self.store_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存生日数据失败: {e}")

    # ================================================================
    #                        定时调度
    # ================================================================

    def _schedule_jobs(self):
        """安排每日定时任务：检查生日 + 发送祝福"""
        if not self.birthday_check_enabled:
            logger.info("❌ 生日祝福功能已禁用")
            return

        # 每天 check_hour:check_minute 检查好友生日
        self.scheduler.add_job(
            self._daily_check_birthdays,
            trigger=CronTrigger(hour=self.check_hour, minute=self.check_minute),
            name="daily_birthday_check",
            misfire_grace_time=600,
        )
        logger.info(f"✅ 已安排每日生日检查: {self.check_hour}:{self.check_minute:02d}")

        # 每天 send_hour:send_minute 发送祝福
        self.scheduler.add_job(
            self._daily_send_greetings,
            trigger=CronTrigger(hour=self.send_hour, minute=self.send_minute),
            name="daily_birthday_greet",
            misfire_grace_time=600,
        )
        logger.info(f"✅ 已安排每日生日祝福: {self.send_hour}:{self.send_minute:02d}")

    # ================================================================
    #                  获取客户端与好友列表
    # ================================================================

    async def _get_client(self):
        """获取可用的 aiocqhttp 客户端"""
        platforms = self.context.platform_manager.get_insts()
        for platform in platforms:
            if hasattr(platform, "get_client"):
                client = platform.get_client()
                if client:
                    return client
        return None

    async def _get_friend_list(self, client) -> list[dict]:
        """获取好友列表"""
        try:
            friends = await client.get_friend_list()
            logger.info(f"👥 获取好友列表成功，共 {len(friends)} 个好友")
            return friends
        except Exception as e:
            logger.error(f"获取好友列表失败: {e}")
            return []

    # ================================================================
    #                     获取用户生日
    # ================================================================

    async def _get_user_birthday(self, client, user_id: int) -> tuple[int, int] | None:
        """
        获取用户生日 (月, 日)。
        兼容多种 OneBot 实现 (NapCat / LLOneBot / go-cqhttp 等)。
        返回 (month, day) 或 None。
        """
        # 方法1: get_stranger_info
        try:
            info = await client.get_stranger_info(user_id=user_id)
            if info:
                m, d = self._extract_birthday_from_info(info)
                if m and d:
                    return (m, d)
        except Exception as e:
            logger.debug(f"get_stranger_info 获取 {user_id} 生日失败: {e}")

        # 方法2: _get_vip_info (go-cqhttp 扩展)
        try:
            vip_info = await client.call_action("_get_vip_info", user_id=user_id)
            if vip_info:
                m, d = self._extract_birthday_from_info(vip_info)
                if m and d:
                    return (m, d)
        except Exception as e:
            logger.debug(f"_get_vip_info 获取 {user_id} 生日失败: {e}")

        # 方法3: get_user_info (NapCat 扩展)
        try:
            user_info = await client.call_action("get_user_info", user_id=user_id)
            if user_info:
                m, d = self._extract_birthday_from_info(user_info)
                if m and d:
                    return (m, d)
        except Exception as e:
            logger.debug(f"get_user_info 获取 {user_id} 生日失败: {e}")

        return None

    def _extract_birthday_from_info(self, info: dict) -> tuple[int | None, int | None]:
        """
        从用户信息字典中提取生日月日。
        兼容多种返回格式：
          - birthday_month / birthday_day 分开字段
          - birthday: "YYYY-MM-DD" / "MM-DD" / "YYYYMMDD" / "MMDD"
          - birthday: 时间戳
          - birthday: {"year":..., "month":..., "day":...}
          - birth_month / birth_day
        """
        birthday_month = None
        birthday_day = None

        # 格式1: 分开的字段
        if "birthday_month" in info and "birthday_day" in info:
            m = info["birthday_month"]
            d = info["birthday_day"]
            if m and d and int(m) > 0 and int(d) > 0:
                return (int(m), int(d))

        # 格式2: birthday 字段
        birthday_raw = info.get("birthday", info.get("Birthday", None))
        if birthday_raw:
            if isinstance(birthday_raw, str) and birthday_raw.strip():
                bday = birthday_raw.strip()
                if "-" in bday:
                    parts = bday.split("-")
                    if len(parts) == 3:
                        try:
                            birthday_month = int(parts[1])
                            birthday_day = int(parts[2])
                        except ValueError:
                            pass
                    elif len(parts) == 2:
                        try:
                            birthday_month = int(parts[0])
                            birthday_day = int(parts[1])
                        except ValueError:
                            pass
                elif len(bday) == 8 and bday.isdigit():
                    try:
                        birthday_month = int(bday[4:6])
                        birthday_day = int(bday[6:8])
                    except ValueError:
                        pass
                elif len(bday) == 4 and bday.isdigit():
                    try:
                        birthday_month = int(bday[0:2])
                        birthday_day = int(bday[2:4])
                    except ValueError:
                        pass
            elif isinstance(birthday_raw, (int, float)) and birthday_raw > 0:
                try:
                    bday_dt = datetime.fromtimestamp(birthday_raw)
                    birthday_month = bday_dt.month
                    birthday_day = bday_dt.day
                except Exception:
                    pass
            elif isinstance(birthday_raw, dict):
                m = birthday_raw.get("month", 0)
                d = birthday_raw.get("day", 0)
                if m and d:
                    birthday_month = int(m)
                    birthday_day = int(d)

        # 格式3: birth_month / birth_day
        if not birthday_month or not birthday_day:
            m = info.get("birth_month", 0)
            d = info.get("birth_day", 0)
            if m and d:
                birthday_month = int(m)
                birthday_day = int(d)

        if (
            birthday_month
            and birthday_day
            and 1 <= birthday_month <= 12
            and 1 <= birthday_day <= 31
        ):
            return (birthday_month, birthday_day)

        return (None, None)

    # ================================================================
    #                     每日检查生日
    # ================================================================

    async def _daily_check_birthdays(self):
        """每天定时遍历所有好友，获取并缓存生日信息"""
        if self.is_checking:
            logger.warning("⚠️ 已有生日检查任务在执行中")
            return

        self.is_checking = True
        try:
            now = datetime.now(self.timezone)
            today_str = now.strftime("%Y-%m-%d")
            logger.info(f"🔍 开始检查好友生日... ({today_str})")

            client = await self._get_client()
            if not client:
                logger.error("❌ 未找到可用的客户端，无法检查生日")
                return

            friends = await self._get_friend_list(client)
            if not friends:
                logger.warning("⚠️ 好友列表为空")
                return

            birthday_count = 0
            today_birthday_friends = []

            for friend in friends:
                user_id = str(friend.get("user_id", ""))
                nickname = friend.get("nickname", "未知")

                if not user_id or self._is_blacklisted(user_id):
                    continue

                birthday = await self._get_user_birthday(client, int(user_id))

                if birthday:
                    month, day = birthday
                    self.birthday_cache[user_id] = {
                        "nickname": nickname,
                        "month": month,
                        "day": day,
                    }
                    birthday_count += 1

                    if month == now.month and day == now.day:
                        today_birthday_friends.append(
                            {"user_id": user_id, "nickname": nickname}
                        )

                # 避免请求过快被风控
                await asyncio.sleep(0.5)

            self.last_check_date = today_str
            self._save_store_data()

            logger.info(f"✅ 生日检查完成: 获取到 {birthday_count} 个好友生日")

            if today_birthday_friends:
                names = ", ".join([f["nickname"] for f in today_birthday_friends])
                logger.info(f"🎂 今天过生日的好友: {names}")

                notify_msg = (
                    f"🎂 今日生日提醒\n"
                    f"📅 日期: {today_str}\n"
                    f"🎉 今天过生日的好友:\n"
                    + "\n".join(
                        [
                            f"  • {f['nickname']} ({f['user_id']})"
                            for f in today_birthday_friends
                        ]
                    )
                    + f"\n\n⏰ 将在 {self.send_hour}:{self.send_minute:02d} 发送祝福"
                )
                await self._send_group_notification(notify_msg)
            else:
                logger.info("📭 今天没有好友过生日")

        except Exception as e:
            logger.error(f"生日检查失败: {e}", exc_info=True)
        finally:
            self.is_checking = False

    # ================================================================
    #                     发送生日祝福
    # ================================================================

    async def _daily_send_greetings(self):
        """在指定时间向今天过生日的好友发送私聊祝福"""
        try:
            now = datetime.now(self.timezone)
            today_str = now.strftime("%Y-%m-%d")

            greeted_list = self.greeted_today.get(today_str, [])

            # 如果缓存为空且今天还没检查过，先执行一次检查
            if not self.birthday_cache and self.last_check_date != today_str:
                logger.info("📋 生日缓存为空，先执行一次检查...")
                await self._daily_check_birthdays()

            # 找出今天过生日且尚未祝福的好友
            birthday_friends = []
            for user_id, info in self.birthday_cache.items():
                if (
                    info.get("month") == now.month
                    and info.get("day") == now.day
                    and user_id not in greeted_list
                    and not self._is_blacklisted(user_id)
                ):
                    birthday_friends.append(
                        {
                            "user_id": user_id,
                            "nickname": info.get("nickname", "好友"),
                        }
                    )

            if not birthday_friends:
                logger.info("📭 今天没有需要祝福的好友（或已全部祝福）")
                return

            client = await self._get_client()
            if not client:
                logger.error("❌ 未找到可用的客户端，无法发送祝福")
                return

            logger.info(f"🎉 开始发送生日祝福，共 {len(birthday_friends)} 位好友")

            success_count = 0
            fail_count = 0

            for friend in birthday_friends:
                user_id = friend["user_id"]
                nickname = friend["nickname"]

                try:
                    greeting = await self._generate_greeting(nickname)

                    await client.send_private_msg(
                        user_id=int(user_id), message=greeting
                    )

                    if today_str not in self.greeted_today:
                        self.greeted_today[today_str] = []
                    self.greeted_today[today_str].append(user_id)

                    success_count += 1
                    logger.info(f"🎂 已向 {nickname}({user_id}) 发送生日祝福")

                except Exception as e:
                    fail_count += 1
                    logger.error(f"❌ 向 {nickname}({user_id}) 发送祝福失败: {e}")

                await asyncio.sleep(self.greeting_interval)

            self._cleanup_greeted_data()
            self._save_store_data()

            complete_msg = (
                f"✅ 生日祝福发送完成\n"
                f"🎉 成功: {success_count} 人\n"
                f"❌ 失败: {fail_count} 人"
            )
            await self._send_group_notification(complete_msg)
            logger.info(f"✅ 祝福完成: 成功 {success_count}, 失败 {fail_count}")

        except Exception as e:
            logger.error(f"发送生日祝福失败: {e}", exc_info=True)

    def _cleanup_greeted_data(self):
        """清理过期的已祝福记录，只保留最近3天"""
        now = datetime.now(self.timezone)
        cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        keys_to_remove = [k for k in self.greeted_today if k < cutoff]
        for k in keys_to_remove:
            del self.greeted_today[k]

    # ================================================================
    #              LLM 生成祝福 (适配人格提示词)
    # ================================================================

    async def _generate_greeting(self, nickname: str) -> str:
        """生成生日祝福：优先使用 LLM + 人格提示词，失败回退默认"""
        if not self.use_llm_greeting:
            return self._get_default_greeting(nickname)

        try:
            greeting = await self._generate_greeting_with_llm(nickname)
            if greeting:
                return greeting
        except Exception as e:
            logger.error(f"LLM 生成祝福失败，使用默认祝福: {e}")

        return self._get_default_greeting(nickname)

    async def _generate_greeting_with_llm(self, nickname: str) -> str | None:
        """
        使用 AstrBot 的 LLM Provider + 人格提示词生成祝福。

        参考官方文档:
          prov = self.context.get_using_provider()
          llm_resp = await prov.text_chat(
              prompt="...",
              contexts=[],
              system_prompt="..."
          )
        """
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("没有可用的 LLM Provider，无法生成 AI 祝福")
            return None

        # 获取人格提示词
        persona_prompt = self._get_persona_prompt()

        # 拼接 system_prompt: 人格 + 生日祝福指令
        system_prompt = ""
        if persona_prompt:
            system_prompt = persona_prompt + "\n\n"

        system_prompt += (
            "现在你需要为一位好友送上生日祝福。"
            "请完全使用你自己的性格和说话风格来表达祝福，"
            "要真诚、温暖、有个性，不要太官方和生硬。"
            "祝福内容控制在2-4句话，可以适当加入emoji表情。"
            "直接说祝福内容即可，不要加任何前缀说明。"
        )

        user_prompt = (
            f"今天是你的好友「{nickname}」的生日！"
            f"请为ta送上一段温馨又有个性的生日祝福吧~"
        )

        try:
            llm_response = await provider.text_chat(
                prompt=user_prompt,
                contexts=[],
                system_prompt=system_prompt,
            )

            if llm_response and llm_response.completion_text:
                result = llm_response.completion_text.strip()
                if result:
                    logger.debug(f"LLM 生成祝福成功: {result[:50]}...")
                    return result
        except Exception as e:
            logger.error(f"调用 LLM text_chat 失败: {e}")

        return None

    def _get_persona_prompt(self) -> str:
        """
        获取 AstrBot 当前配置的人格提示词 (Persona)。

        参考官方文档:
          from astrbot.api.provider import Personality
          personas = self.context.provider_manager.personas  # List[Personality]
        """
        # 方法1: 通过 provider_manager.personas 获取 (推荐)
        try:
            if hasattr(self.context, "provider_manager") and self.context.provider_manager:
                personas = self.context.provider_manager.personas
                if personas and len(personas) > 0:
                    persona = personas[0]
                    prompt = ""
                    if hasattr(persona, "prompt"):
                        prompt = persona.prompt
                    elif isinstance(persona, dict):
                        prompt = persona.get("prompt", "")
                    if prompt:
                        logger.debug(f"获取到人格提示词 (provider_manager): {prompt[:50]}...")
                        return prompt
        except Exception as e:
            logger.debug(f"通过 provider_manager 获取人格失败: {e}")

        # 方法2: 从配置中获取
        try:
            astrbot_config = self.context.get_config()
            if astrbot_config:
                # 尝试 personality 字段
                personality = astrbot_config.get("personality", [])
                if personality and isinstance(personality, list) and len(personality) > 0:
                    p = personality[0]
                    if isinstance(p, dict):
                        prompt = p.get("prompt", "")
                        if prompt:
                            return prompt
                    elif isinstance(p, str):
                        return p

                # 尝试 provider_settings.prompt
                provider_settings = astrbot_config.get("provider_settings", {})
                if isinstance(provider_settings, dict):
                    prompt = provider_settings.get("prompt", "")
                    if prompt:
                        return prompt
        except Exception as e:
            logger.debug(f"从配置获取人格失败: {e}")

        return ""

    def _get_default_greeting(self, nickname: str) -> str:
        """获取默认（非LLM）祝福消息"""
        greeting = random.choice(DEFAULT_GREETINGS)
        return f"@{nickname}\n{greeting}"

    # ================================================================
    #                        工具方法
    # ================================================================

    def _is_blacklisted(self, user_id: str) -> bool:
        """检查用户是否在黑名单中"""
        return str(user_id) in self.blacklist_users

    async def _send_group_notification(self, message: str):
        """发送群通知"""
        if not self.notify_groups:
            return
        try:
            client = await self._get_client()
            if client:
                for group_id in self.notify_groups:
                    try:
                        await client.send_group_msg(
                            group_id=int(group_id), message=message
                        )
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.error(f"发送群通知到 {group_id} 失败: {e}")
        except Exception as e:
            logger.error(f"发送群通知失败: {e}")

    # ================================================================
    #                      管理员命令
    # ================================================================
    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("测试生日")
    async def test_birthday(self, event: AiocqhttpMessageEvent, qq: str):
        """测试能否获取指定QQ的生日字段: /测试生日 <QQ号>"""
        client = event.bot
        results = []

        # 方法1: get_stranger_info
        try:
            info = await client.get_stranger_info(user_id=int(qq))
            results.append(f"📋 get_stranger_info:\n{json.dumps(info, ensure_ascii=False, indent=2)}")
        except Exception as e:
            results.append(f"❌ get_stranger_info 失败: {e}")

        # 方法2: get_user_info (NapCat扩展)
        try:
            info = await client.call_action("get_user_info", user_id=int(qq))
            results.append(f"📋 get_user_info:\n{json.dumps(info, ensure_ascii=False, indent=2)}")
        except Exception as e:
            results.append(f"❌ get_user_info 失败: {e}")

        # 方法3: _get_vip_info (go-cqhttp扩展)
        try:
            info = await client.call_action("_get_vip_info", user_id=int(qq))
            results.append(f"📋 _get_vip_info:\n{json.dumps(info, ensure_ascii=False, indent=2)}")
        except Exception as e:
            results.append(f"❌ _get_vip_info 失败: {e}")

        yield event.plain_result("\n\n".join(results))
    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("生日检查")
    async def manual_check(self, event: AiocqhttpMessageEvent):
        """手动触发生日检查"""
        if self.is_checking:
            yield event.plain_result("⚠️ 已有检查任务在执行中，请稍后")
            return
        yield event.plain_result("🔍 开始手动检查好友生日...")
        asyncio.create_task(self._daily_check_birthdays())

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("立即祝福")
    async def manual_greet(self, event: AiocqhttpMessageEvent):
        """手动触发发送生日祝福"""
        yield event.plain_result("🎂 开始手动发送生日祝福...")
        asyncio.create_task(self._daily_send_greetings())

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("生日状态")
    async def birthday_status(self, event: AiocqhttpMessageEvent):
        """查看生日插件状态"""
        now = datetime.now(self.timezone)
        today_str = now.strftime("%Y-%m-%d")

        today_birthdays = []
        for user_id, info in self.birthday_cache.items():
            if info.get("month") == now.month and info.get("day") == now.day:
                today_birthdays.append(info.get("nickname", user_id))

        greeted_today = self.greeted_today.get(today_str, [])

        status_info = (
            f"🎂 生日祝福插件状态\n"
            f"✅ 功能状态: {'开启' if self.birthday_check_enabled else '关闭'}\n"
            f"📅 上次检查: {self.last_check_date}\n"
            f"🔍 检查时间: 每天 {self.check_hour}:{self.check_minute:02d}\n"
            f"🎉 祝福时间: 每天 {self.send_hour}:{self.send_minute:02d}\n"
            f"🤖 LLM祝福: {'开启' if self.use_llm_greeting else '关闭'}\n"
            f"📊 已缓存生日: {len(self.birthday_cache)} 人\n"
            f"🎂 今日寿星: {len(today_birthdays)} 人"
        )

        if today_birthdays:
            status_info += f"\n   → {', '.join(today_birthdays)}"

        status_info += (
            f"\n✉️ 今日已祝福: {len(greeted_today)} 人\n"
            f"🚫 黑名单: {len(self.blacklist_users)} 人\n"
            f"📢 通知群组: {len(self.notify_groups)} 个"
        )

        yield event.plain_result(status_info)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("查看寿星")
    async def view_birthday_friends(self, event: AiocqhttpMessageEvent):
        """查看今天过生日的好友"""
        now = datetime.now(self.timezone)
        today_str = now.strftime("%Y-%m-%d")

        today_birthdays = []
        for user_id, info in self.birthday_cache.items():
            if info.get("month") == now.month and info.get("day") == now.day:
                today_birthdays.append(
                    f"• {info.get('nickname', '未知')} ({user_id})"
                )

        if not today_birthdays:
            yield event.plain_result("📭 今天没有好友过生日")
            return

        result = f"🎂 今日寿星 ({today_str}):\n" + "\n".join(today_birthdays)
        yield event.plain_result(result)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("查看生日缓存")
    async def view_birthday_cache(self, event: AiocqhttpMessageEvent):
        """查看已缓存的生日信息（按月日排序）"""
        if not self.birthday_cache:
            yield event.plain_result("📭 生日缓存为空，请先执行 /生日检查")
            return

        sorted_cache = sorted(
            self.birthday_cache.items(),
            key=lambda x: (x[1].get("month", 0), x[1].get("day", 0)),
        )

        display = sorted_cache[:30]
        lines = []
        for user_id, info in display:
            m = info.get("month", 0)
            d = info.get("day", 0)
            name = info.get("nickname", "未知")
            lines.append(f"• {m:02d}-{d:02d} {name} ({user_id})")

        result = f"📋 生日缓存 ({len(self.birthday_cache)} 人):\n" + "\n".join(lines)
        if len(self.birthday_cache) > 30:
            result += f"\n... 等共 {len(self.birthday_cache)} 人"

        yield event.plain_result(result)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("手动添加生日")
    async def manual_add_birthday(
        self, event: AiocqhttpMessageEvent, user_id: str, month: str, day: str
    ):
        """手动添加好友生日: /手动添加生日 <QQ号> <月> <日>"""
        if not user_id.isdigit():
            yield event.plain_result("❌ 请输入正确的QQ号")
            return

        try:
            m = int(month)
            d = int(day)
            if not (1 <= m <= 12 and 1 <= d <= 31):
                raise ValueError
        except ValueError:
            yield event.plain_result("❌ 请输入正确的月份(1-12)和日期(1-31)")
            return

        nickname = user_id
        try:
            client = await self._get_client()
            if client:
                info = await client.get_stranger_info(user_id=int(user_id))
                nickname = info.get("nickname", user_id)
        except Exception:
            pass

        self.birthday_cache[user_id] = {
            "nickname": nickname,
            "month": m,
            "day": d,
        }
        self._save_store_data()

        yield event.plain_result(
            f"✅ 已添加生日: {nickname}({user_id}) - {m:02d}月{d:02d}日"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("删除生日")
    async def remove_birthday(self, event: AiocqhttpMessageEvent, user_id: str):
        """删除好友生日记录: /删除生日 <QQ号>"""
        if user_id in self.birthday_cache:
            info = self.birthday_cache.pop(user_id)
            self._save_store_data()
            yield event.plain_result(
                f"✅ 已删除 {info.get('nickname', user_id)} 的生日记录"
            )
        else:
            yield event.plain_result(f"❌ 未找到 {user_id} 的生日记录")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("添加生日黑名单")
    async def add_blacklist(self, event: AiocqhttpMessageEvent, user_id: str):
        """添加用户到生日黑名单"""
        if not user_id.isdigit():
            yield event.plain_result("❌ 请输入正确的QQ号")
            return

        if user_id in self.blacklist_users:
            yield event.plain_result(f"❌ 用户 {user_id} 已在黑名单中")
            return

        self.blacklist_users.append(user_id)
        self.config["blacklist_users"] = self.blacklist_users
        self.config.save_config()
        yield event.plain_result(f"✅ 已添加 {user_id} 到生日黑名单")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("移除生日黑名单")
    async def remove_blacklist(self, event: AiocqhttpMessageEvent, user_id: str):
        """从生日黑名单移除用户"""
        if user_id not in self.blacklist_users:
            yield event.plain_result(f"❌ 用户 {user_id} 不在黑名单中")
            return

        self.blacklist_users.remove(user_id)
        self.config["blacklist_users"] = self.blacklist_users
        self.config.save_config()
        yield event.plain_result(f"✅ 已从生日黑名单移除 {user_id}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("查看生日黑名单")
    async def view_blacklist(self, event: AiocqhttpMessageEvent):
        """查看生日黑名单"""
        if not self.blacklist_users:
            yield event.plain_result("📝 生日黑名单为空")
            return

        blacklist_str = "\n".join([f"• {uid}" for uid in self.blacklist_users])
        yield event.plain_result(
            f"📋 生日黑名单 ({len(self.blacklist_users)} 人):\n{blacklist_str}"
        )

    # ================================================================
    #                        生命周期
    # ================================================================

    async def terminate(self):
        """插件卸载时停止调度器"""
        self.scheduler.shutdown()
        logger.info("🛑 生日祝福插件已停止")