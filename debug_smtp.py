# #!/usr/bin/env python3
# from aiosmtpd.controller import Controller
# import asyncio, sys

# class PrintHandler:
#     async def handle_DATA(self, server, session, envelope):
#         print("----- SMTP MESSAGE -----")
#         print(envelope.content.decode('utf8', errors='replace'))
#         print("----- END MESSAGE -----")
#         return '250 Message accepted for delivery'

# def main():
#     loop = asyncio.new_event_loop()
#     asyncio.set_event_loop(loop)
#     ctrl = Controller(PrintHandler(), hostname='127.0.0.1', port=1025)
#     ctrl.start()
#     print("Debug SMTP server running on 127.0.0.1:1025 (Ctrl-C to stop)")
#     try:
#         loop.run_forever()
#     except KeyboardInterrupt:
#         ctrl.stop()
#         sys.exit(0)

# if __name__ == "__main__":
#     main()
