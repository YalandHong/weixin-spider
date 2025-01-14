# -*- encoding: utf-8 -*-
# !/usr/bin/python3
# @File   : wx_monitor.py
from codecs import replace_errors
import json
import re
import time
import threading
import redis
import hashlib
import sys

from pyquery import PyQuery

from api import get_history_api, get_html_api, get_article_comments_api, split_article_url2mis, \
    get_article_read_like_api
from tools.handle import WeChatWnd
from webapp import models
from webapp import db
from exceptions import NoneKeyUinError, KeyExpireError, ArticleHasBeenDeleteError, IPError
from settings import SLEEP_TIME, WX_REDIS_CONFIG, WX_CHAT_WND_NAME, WX_UPDATE_TIME, WX_NOT_UPDATE_TIME, UPDATE_DELAY, \
    UPDATE_STOP, MONITOR_ERROR


def delete_key_uin(account_biz):
    redis_server = redis.StrictRedis(connection_pool=redis.ConnectionPool(
        **WX_REDIS_CONFIG))
    hash_key = hashlib.md5(account_biz.encode("utf-8")).hexdigest()
    redis_server.delete(hash_key)


def _get_key_uin(account_biz):
    redis_server = redis.StrictRedis(connection_pool=redis.ConnectionPool(
        **WX_REDIS_CONFIG))
    hash_key = hashlib.md5(account_biz.encode("utf-8")).hexdigest()
    return redis_server.get(hash_key)


def get_key_uin(account_biz):
    key_uin = _get_key_uin(account_biz)
    if not key_uin:
        raise NoneKeyUinError("not key_uin: {}".format(key_uin))
    #print("get_key_uin: ", key_uin)
    key_uin_dict = json.loads(key_uin, encoding="utf-8")
    if not key_uin_dict.get("key", None) or not key_uin_dict.get("uin", None):
        raise NoneKeyUinError("not get: {}".format(key_uin_dict))
    return key_uin_dict


def check_key_uin(account_biz):
    try:
        key_uin_dict = get_key_uin(account_biz)
        get_history_api(
            **{
                "key": key_uin_dict.get("key", ""),
                "uin": key_uin_dict.get("uin", ""),
                "biz": account_biz,
                "offset": 0,
            })
    except KeyExpireError:
        delete_key_uin(account_biz)
        raise NoneKeyUinError(f"key: 已过期")


def get_pass_key_and_uin(article_url: str, account_biz: str):
    wx_chat = WeChatWnd(WX_CHAT_WND_NAME)
    key_uin = _get_key_uin(account_biz)

    while not key_uin:
        try:
            wx_chat.send_msg(article_url)
        except Exception as e:
            print(e.args)
            time.sleep(0.2)
        finally:
            #time.sleep(2)
            wx_chat.close_web()  # 暂时不确定 关闭窗口 或者不关闭 的影响
            time.sleep(2)
            key_uin = _get_key_uin(account_biz)

    return json.loads(key_uin, encoding="utf-8")


class _MonitorThread(threading.Thread):

    @staticmethod
    def update_obj(obj, **kwargs):
        for k, v in kwargs.items():
            setattr(obj, k, v)
        db.session.add(obj)
        db.session.commit()

    @staticmethod
    def articles(**filter_by):
        article_list = models.Article.query.filter_by(**filter_by).order_by(
            models.Article.article_publish_time.desc()).all()
        db.session.commit()
        print("Article Len:", len(article_list))
        return article_list

    @staticmethod
    def accounts(**filter_by):
        account_list = models.Account.query.filter_by(**filter_by).order_by(
            models.Account.created.desc()).all()
        db.session.commit()
        return account_list

    @staticmethod
    def check_account_status(_id: int, status: int):
        # print("check_account_status", _id, status)
        status_flag = models.Account.query.get(_id).status == status
        db.session.commit()
        return status_flag

    def run(self):
        self.setName(self.__class__.__name__)
        try:
            self.start_run()
        except Exception as e:
            print(repr(e))

    def start_run(self):
        pass


class History(_MonitorThread):

    def update_account(self, account, **kwargs):
        self.update_obj(account, **kwargs)

    def update_article(self, article, **kwargs):
        self.update_obj(article, **kwargs)

    def load_accounts(self, **filter_by):
        account_list = self.accounts(**filter_by)
        print("full account list:", account_list)
        return [
            account for account in account_list if account.status in [1, 2]
        ]

    @staticmethod
    def save_article(account_id, article_item):
        counts = models.Article.query.filter_by(
            article_content_url=article_item["article_content_url"],
            article_publish_time=article_item["article_publish_time"],
        ).count()
        new_article = False
        if counts == 0:
            article = models.Article(**article_item, account_id=account_id)
            print(article)
            db.session.add(article)
            db.session.commit()
            new_article = True
        return new_article

    def account_run(self, account_id):
        print("account run: ", account_id)
        account = models.Account.query.get(account_id)
        account_biz = account.account_biz
        account_offset = account.offset
        key_uin_dict = get_key_uin(account_biz)
        offset = 0
        one_add = False
        while 1:
            if not self.check_account_status(account_id, 2):
                break
            s_time = time.time()
            try:
                histories = get_history_api(
                    **{
                        "key": key_uin_dict.get("key", ""),
                        "uin": key_uin_dict.get("uin", ""),
                        "biz": account_biz,
                        "offset": offset,
                    })
                ending = histories['ending']
                next_offset = histories["next_offset"]
                print(
                    f"biz: {account_biz} offset: {offset} next_offset: {next_offset}"
                )
                article_items = histories["results"]["article_infos"]
                new_article = False
                for article_item in article_items:
                    if not article_item["article_content_url"]:
                        continue
                    if new_article:
                        self.save_article(account_id, article_item)
                    else:
                        new_article = self.save_article(
                            account_id, article_item)

                account.counts = models.Article.query.filter_by(
                    account_id=account.id).count()
                if account_offset == 0:
                    account.offset = offset
                    offset = next_offset
                elif new_article:
                    if one_add:
                        account.offset = offset
                    offset = next_offset
                else:
                    print(
                        f"biz: {account_biz} 当前offset: {offset}文章都存在 next_offset: {next_offset}"
                    )
                    if one_add or account_offset == 0:
                        account.offset = offset
                        offset = next_offset
                    else:
                        offset += account_offset
                        account.offset = offset
                        one_add = True
                if ending:
                    account.offset = offset
                    account.end = True
                self.update_obj(account)
                if ending:
                    break
            except KeyExpireError:
                delete_key_uin(account_biz)
                raise NoneKeyUinError(f"key: 已过期 offset: {offset}")
            # 控制访问频次，以免被禁
            while time.time() - s_time < SLEEP_TIME:
                time.sleep(0.1)

    def start_run(self):
        account_list = self.load_accounts()
        print("account list:", account_list)
        for account in account_list:
            account_id = account.id
            account_biz = account.account_biz
            get_key_uin(account_biz)
            print("开始同步；", account)
            self.update_account(account, status=2)
            self.account_run(account_id)
            self.update_account(account,
                                status=0,
                                update=str(int(time.time())))
            print("数据已同步；", account)
            if self.check_account_status(account_id, 2):
                self.update_account(account, status=1)


class Article(_MonitorThread):

    @staticmethod
    def get_comment_id_from_html(res_html):
        return re.search(r"comment_id = .*?\"([\d]+)\"", res_html).group(1)

    @staticmethod
    def get_content_from_html(res_html):
        # return re.search(r"(.*)", res_html).group(1)
        # print(PyQuery(res_html)("#js_content").html())
        # js_content = PyQuery(res_html)("#js_content").html().replace("\n", "").strip()
        # js_content = re.sub(r'data-src', "src", js_content)
        return str(PyQuery(res_html)("#js_content")).replace("\n", "").strip()

    def article_run(self, article_id):
        article = models.Article.query.get(article_id)
        article_url = article.article_content_url
        account_biz = models.Account.query.get(article.account_id).account_biz
        key_uin_dict = get_key_uin(account_biz)
        key = key_uin_dict.get("key", "")
        uin = key_uin_dict.get("uin", "")
        if key and uin:
            article_url = article_url + '&key=%s&ascene=1&uin=%s' % (key, uin)
        try:
            article_html = get_html_api(article_url)
            comment_id = self.get_comment_id_from_html(article_html)
            article.article_html = self.get_content_from_html(article_html)
            article.article_comment_id = comment_id
            article.article_done = True
        except (ArticleHasBeenDeleteError, AttributeError) as e:
            article.article_fail = True
            article.article_done = True
            print(repr(e))
        # except IPError:
        #     if key and uin:
        #         delete_key_uin(account_biz)
        finally:
            db.session.add(article)
            db.session.commit()
            print("COMMIT", article.id, article.article_done,
                  article.article_title)

    def start_run(self):
        for article in self.articles(article_done=False):
            print("文章开始同步；", article)
            article_id = article.id
            self.article_run(article_id)
            time.sleep(UPDATE_DELAY)


class Comment(_MonitorThread):

    def start_run(self):
        article_list = models.Article.query.filter_by(
            article_done=True, ).filter(
                models.Article.article_comment_id != 0,
                models.Article.comment_update <
                int(time.time()) - WX_UPDATE_TIME,
                models.Article.article_publish_time >
                models.Article.comment_update - WX_NOT_UPDATE_TIME,
            ).all()

        db.session.commit()
        print("Comment len(article_list): ", len(article_list))
        for article in article_list:
            print("文章评论开始同步；", article)
            self.article_run(article.id)
            print("文章评论已同步完成；", article)
            time.sleep(UPDATE_DELAY)

    @staticmethod
    def save_comment(article_id, comment_dict):
        for comment_item in comment_dict['comments']:
            if models.Comment.query.filter_by(
                    content_id=str(comment_item["content_id"])).count() == 0:
                comment = models.Comment(
                    user_name=comment_item["user_name"],
                    user_logo=comment_item["user_logo"],
                    content=comment_item["content"],
                    datetime=str(comment_item["datetime"]),
                    content_id=str(comment_item["content_id"]),
                    like_count=int(comment_item["like_count"]),
                    article_id=article_id)
                db.session.add(comment)
                db.session.commit()
            comment = models.Comment.query.filter_by(
                content_id="%s" % comment_item["content_id"]).first()
            reply_list = comment_item["reply_list"]
            for reply_item in reply_list:
                reply = models.CommentReply(**reply_item,
                                            comment_id=comment.id)
                db.session.add(reply)
                db.session.commit()

    def article_run(self, article_id):
        article = models.Article.query.get(article_id)
        comment_id = article.article_comment_id
        account_biz = models.Account.query.get(article.account_id).account_biz
        key_uin_dict = get_key_uin(account_biz)
        key = key_uin_dict.get("key", "")
        uin = key_uin_dict.get("uin", "")
        pass_ticket = key_uin_dict.get("pass_ticket", "")
        try:
            comment_dict = get_article_comments_api(
                biz=account_biz,
                comment_id=comment_id,
                key=key,
                uin=uin,
                pass_ticket=pass_ticket,
            )['results']
            self.save_comment(article_id, comment_dict)
            comment_count = comment_dict['comment_count']
            article.comment_count = comment_count
            article.comment_update = str(int(time.time()))
            db.session.add(article)
            db.session.commit()
        except KeyExpireError:
            check_key_uin(account_biz)


class ReadLike(_MonitorThread):

    def start_run(self):
        article_list = models.Article.query.filter_by(
            article_done=True, ).filter(
                models.Article.article_fail == False,
                # update时间超过1天，那就要重新获取阅读量
                models.Article.read_like_update <
                int(time.time()) - WX_UPDATE_TIME,
                models.Article.article_publish_time >
                models.Article.read_like_update - WX_NOT_UPDATE_TIME,
            ).all()
        db.session.commit()
        print("ReadLike len(article_list): ", len(article_list))
        for article in article_list:
            print("文章阅读数据开始同步；", article)
            self.article_run(article.id)
            print("文章阅读数据已同步完成；", article)
            time.sleep(UPDATE_DELAY)

    def article_run(self, article_id):
        article = models.Article.query.get(article_id)
        article_url = article.article_content_url
        comment_id = article.article_comment_id
        account_biz = models.Account.query.get(article.account_id).account_biz
        key_uin_dict = get_key_uin(account_biz)
        key = key_uin_dict.get("key", "")
        uin = key_uin_dict.get("uin", "")
        try:
            print(split_article_url2mis(article_url), article_url)
            read_like = get_article_read_like_api(
                biz=account_biz,
                key=key,
                uin=uin,
                comment_id=comment_id,
                **split_article_url2mis(article_url))
            read_like = read_like["results"]
            article.read_count = read_like['read_count']
            article.like_count = read_like['like_count']
            article.read_like_update = str(int(time.time()))
            db.session.add(article)
            db.session.commit()
        except KeyExpireError:
            check_key_uin(account_biz)


if __name__ == '__main__':
    #class_names = ["History", "Article", "Comment", "ReadLike"]
    # class_names = ["History"]
    # class_names = ["Article", "ReadLike"]
    class_names = sys.argv[1:]
    print(class_names)
    thread_list = [globals()[thread_name]() for thread_name in class_names]

    for thread in thread_list:
        thread.start()
        print("thread.start: ", thread.name)
        thread.join()
